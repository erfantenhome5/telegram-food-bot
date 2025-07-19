import asyncio
import hashlib
import json
import logging
import os
import random
import re
import shutil
import traceback
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import sys
import time

import httpx
import sentry_sdk
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.types import Event, Hint
from telethon import Button, TelegramClient, errors, events, types, sessions
from telethon.extensions import markdown
from telethon.tl.functions.channels import (CreateChannelRequest, GetParticipantRequest,
                                            InviteToChannelRequest, LeaveChannelRequest)
from telethon.tl.functions.messages import (ExportChatInviteRequest,
                                            GetAllStickersRequest,
                                            GetStickerSetRequest,
                                            ImportChatInviteRequest,
                                            SendReactionRequest,
                                            SearchStickerSetsRequest)
from telethon.tl.types import (ChannelParticipantCreator, ChannelParticipantsAdmins,
                               InputStickerSetID, InputStickerSetShortName, Message,
                               PeerChannel, ReactionEmoji)

# --- Basic Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_activity.log"),
        logging.StreamHandler()
    ]
)
LOGGER = logging.getLogger(__name__)

# --- Environment Loading ---
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
SENTRY_DSN = os.getenv("SENTRY_DSN")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", OPENROUTER_API_KEY) # Use OpenRouter key as fallback for Gemini


if not all([API_ID, API_HASH, BOT_TOKEN, ENCRYPTION_KEY, ADMIN_USER_ID]):
    raise ValueError("Missing required environment variables. Ensure API_ID, API_HASH, BOT_TOKEN, ENCRYPTION_KEY, and ADMIN_USER_ID are set.")

API_ID = int(API_ID)
ADMIN_USER_ID = int(ADMIN_USER_ID)

SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "sessions"))
SESSIONS_DIR.mkdir(exist_ok=True, parents=True)

# --- Custom Markdown for Spoilers ---
class CustomMarkdown:
    @staticmethod
    def parse(text):
        text, entities = markdown.parse(text)
        for i, e in enumerate(entities):
            if isinstance(e, types.MessageEntityTextUrl):
                if e.url == 'spoiler':
                    entities[i] = types.MessageEntitySpoiler(e.offset, e.length)
                elif e.url.startswith('emoji/'):
                    entities[i] = types.MessageEntityCustomEmoji(e.offset, e.length, int(e.url.split('/')[1]))
        return text, entities

    @staticmethod
    def unparse(text, entities):
        for i, e in enumerate(entities or []):
            if isinstance(e, types.MessageEntityCustomEmoji):
                entities[i] = types.MessageEntityTextUrl(e.offset, e.length, f'emoji/{e.document_id}')
            if isinstance(e, types.MessageEntitySpoiler):
                entities[i] = types.MessageEntityTextUrl(e.offset, e.length, 'spoiler')
        return markdown.unparse(text, entities)


# --- Global Proxy Loading Function ---
def load_proxies_from_file(proxy_file_path: str) -> List[Dict]:
    proxy_list = []
    try:
        with open(proxy_file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                try:
                    host, port = line.split(':', 1)
                    proxy_list.append({
                        'proxy_type': 'http',
                        'addr': host,
                        'port': int(port)
                    })
                except ValueError:
                    LOGGER.warning(f"Skipping malformed proxy line: {line}. Expected format is IP:PORT.")
        LOGGER.info(f"Loaded {len(proxy_list)} proxies from {proxy_file_path}.")
    except FileNotFoundError:
        LOGGER.warning(f"Proxy file '{proxy_file_path}' not found.")
    return proxy_list

# --- Proxy Manager for Global Rate Limiting ---
class ProxyManager:
    """
    Manages proxy selection and enforces a global rate limit (RPM).
    """
    RATE_LIMIT = 480
    TIME_WINDOW = 60

    def __init__(self, proxies: List[Dict]):
        self._proxies = proxies
        self._request_timestamps = deque()
        self._lock = asyncio.Lock()

    async def get_proxy(self) -> Optional[Dict]:
        """
        Returns a proxy while respecting the global rate limit.
        """
        if not self._proxies:
            return None

        async with self._lock:
            now = time.monotonic()
            
            while self._request_timestamps and self._request_timestamps[0] <= now - self.TIME_WINDOW:
                self._request_timestamps.popleft()

            if len(self._request_timestamps) >= self.RATE_LIMIT:
                oldest_request_time = self._request_timestamps[0]
                wait_time = oldest_request_time - (now - self.TIME_WINDOW)
                if wait_time > 0:
                    LOGGER.warning(f"Global proxy rate limit hit. Waiting for {wait_time:.2f} seconds.")
                    await asyncio.sleep(wait_time)
            
            self._request_timestamps.append(time.monotonic())
            return random.choice(self._proxies)

# --- Centralized Configuration ---
class Config:
    """Holds all configurable values and UI strings for the bot."""
    MAX_CONCURRENT_WORKERS = 5
    GROUPS_TO_CREATE = 50
    MIN_SLEEP_SECONDS = 144
    MAX_SLEEP_SECONDS = 288
    PROXY_FILE = "proxy.txt"
    PROXY_TIMEOUT = 15
    DAILY_MESSAGE_LIMIT_PER_GROUP = 20
    MESSAGE_SEND_DELAY_MIN = 1
    MESSAGE_SEND_DELAY_MAX = 5
    GROUP_HEALTH_CHECK_INTERVAL_SECONDS = 604800 # 7 days
    AI_REQUEST_TIMEOUT = 20

    PREDEFINED_FALLBACK_MESSAGES = [
        "سلام دوستان!", "چه خبر؟", "کسی اینجا هست؟", "🤔", "👍",
        "عالیه!", "موافقم.", "جالبه.", "چه روز خوبی!", "امیدوارم همگی خوب باشید."
    ]

    PERSONAS = [
        "یک فرد بسیار مشتاق و با انگیزه که همیشه در مورد موفقیت و اهداف صحبت می کند.",
        "یک فرد شوخ طبع و بامزه که سعی می کند با جوک و داستان های خنده دار دیگران را بخنداند.",
        "یک فرد کنجکاو و اهل فن که به تکنولوژی و گجت های جدید علاقه دارد.",
    ]
    
    USER_AGENTS = [
        {'device_model': 'iPhone 15 Pro Max', 'system_version': '17.5.1'},
        {'device_model': 'Samsung Galaxy S24 Ultra', 'system_version': 'SDK 34'},
        {'device_model': 'Google Pixel 8 Pro', 'system_version': 'SDK 34'},
    ]

    BTN_MANAGE_ACCOUNTS = "👤 مدیریت حساب‌ها"
    BTN_SERVER_STATUS = "📊 وضعیت سرور"
    BTN_HELP = "ℹ️ راهنما"
    BTN_SETTINGS = "⚙️ تنظیمات"
    BTN_ADD_ACCOUNT = "➕ افزودن حساب (API)"
    BTN_ADD_ACCOUNT_SELENIUM = "✨ افزودن حساب (مرورگر امن)"
    BTN_BACK = "⬅️ بازگشت"
    BTN_START_PREFIX = "🟢 شروع برای"
    BTN_STOP_PREFIX = "⏹️ توقف برای"
    BTN_DELETE_PREFIX = "🗑️ حذف"
    BTN_SET_KEYWORDS = "📝 تنظیم کلمات کلیدی AI"
    BTN_SET_STICKERS = "🎨 تنظیم استیکرها"
    BTN_SET_CONVERSATION_ACCOUNTS = "🗣️ تنظیم حساب‌های گفتگو"
    BTN_JOIN_VIA_LINK = "🔗 عضویت با لینک"
    BTN_EXPORT_LINKS = "🔗 صدور لینک‌های گروه"
    BTN_FORCE_CONVERSATION = "💬 شروع مکالمه دستی"
    BTN_STOP_FORCE_CONVERSATION = "⏹️ توقف مکالمه دستی"
    BTN_MANUAL_HEALTH_CHECK = "🩺 بررسی سلامت گروه‌ها"

    MSG_WELCOME = "**🤖 به ربات سازنده گروه خوش آمدید!**"
    MSG_ACCOUNT_MENU_HEADER = "👤 **مدیریت حساب‌ها**"
    MSG_HELP_TEXT = "راهنمای جامع ربات..."
    # Other messages can be added here for consistency
    
class SessionManager:
    """Manages encrypted user session files."""
    def __init__(self, fernet: Fernet, directory: Path):
        self._fernet = fernet
        self._dir = directory
        self._user_sessions_dir = self._dir / "user_sessions"
        self._user_sessions_dir.mkdir(exist_ok=True)

    def _get_user_dir(self, user_id: int) -> Path:
        user_dir = self._user_sessions_dir / str(user_id)
        user_dir.mkdir(exist_ok=True)
        return user_dir

    def get_all_accounts(self) -> Dict[str, int]:
        """Returns a dictionary of all accounts across all users."""
        all_accounts = {}
        for user_dir in self._user_sessions_dir.iterdir():
            if user_dir.is_dir():
                try:
                    user_id = int(user_dir.name)
                    accounts = [f.stem for f in user_dir.glob("*.session")]
                    for acc_name in accounts:
                        all_accounts[f"{user_id}:{acc_name}"] = user_id
                except ValueError:
                    continue
        return all_accounts

    def get_user_accounts(self, user_id: int) -> List[str]:
        user_dir = self._get_user_dir(user_id)
        return [f.stem for f in user_dir.glob("*.session")]

    def save_session_string(self, user_id: int, name: str, session_string: str) -> None:
        user_dir = self._get_user_dir(user_id)
        session_file = user_dir / f"{name}.session"
        encrypted_session = self._fernet.encrypt(session_string.encode())
        session_file.write_bytes(encrypted_session)

    def load_session_string(self, user_id: int, name: str) -> Optional[str]:
        user_dir = self._get_user_dir(user_id)
        session_file = user_dir / f"{name}.session"
        if not session_file.exists():
            return None
        try:
            encrypted_session = session_file.read_bytes()
            decrypted_session = self._fernet.decrypt(encrypted_session)
            return decrypted_session.decode()
        except (InvalidToken, IOError):
            LOGGER.error(f"Could not load or decrypt session for {name} of user {user_id}.")
            return None

    def delete_session_file(self, user_id: int, name: str) -> bool:
        user_dir = self._get_user_dir(user_id)
        session_file = user_dir / f"{name}.session"
        if session_file.exists():
            session_file.unlink()
            return True
        return False


class GroupCreatorBot:
    """A class to encapsulate the bot's logic for managing multiple accounts."""

    def __init__(self, session_manager) -> None:
        """Initializes the bot instance and the encryption engine."""
        self.bot = TelegramClient('bot_session', API_ID, API_HASH)
        self.user_sessions: Dict[int, Dict[str, Any]] = {}
        self.active_workers: Dict[str, asyncio.Task] = {}
        self.active_conversations: Dict[str, asyncio.Task] = {}
        self.health_check_lock = asyncio.Lock()
        
        self.config_file = SESSIONS_DIR / "config.json"
        self.config = self._load_json_file(self.config_file, {})
        self.update_config_from_file()

        self.worker_semaphore = asyncio.Semaphore(self.config.get("MAX_CONCURRENT_WORKERS", 5))
        
        # Load all data stores
        self.proxies = load_proxies_from_file(self.config.get("PROXY_FILE", "proxy.txt"))
        self.proxy_manager = ProxyManager(self.proxies)
        self.account_proxies = self._load_json_file(SESSIONS_DIR / "account_proxies.json", {})
        self.known_users = self._load_json_file(SESSIONS_DIR / "known_users.json", [])
        self.created_groups = self._load_json_file(SESSIONS_DIR / "created_groups.json", {})
        self.user_keywords = self._load_json_file(SESSIONS_DIR / "keywords.json", {})
        self.conversation_accounts = self._load_json_file(SESSIONS_DIR / "conversation_accounts.json", {})
        
        try:
            fernet = Fernet(ENCRYPTION_KEY.encode())
            self.session_manager = session_manager(fernet, SESSIONS_DIR)
        except (ValueError, TypeError):
            raise ValueError("Invalid ENCRYPTION_KEY. Please generate a valid key.")

    def update_config_from_file(self):
        """Update runtime config attributes from the loaded JSON."""
        self.max_workers = self.config.get("MAX_CONCURRENT_WORKERS", Config.MAX_CONCURRENT_WORKERS)
        # Add other config reloads here if necessary

    def _load_json_file(self, file_path: Path, default_type: Any = {}) -> Any:
        if not file_path.exists():
            return default_type
        try:
            with file_path.open("r", encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            LOGGER.error(f"Could not read or parse {file_path.name}. Starting with empty data.")
            return default_type

    def _save_json_file(self, data: Any, file_path: Path) -> None:
        try:
            with file_path.open("w", encoding='utf-8') as f:
                json.dump(data, f, indent=4)
        except IOError:
            LOGGER.error(f"Could not save {file_path.name}.")

    async def _create_worker_client(self, session_string: str, proxy: Optional[Dict]) -> Optional[TelegramClient]:
        session = sessions.StringSession(session_string)
        device_params = random.choice(Config.USER_AGENTS)

        client = TelegramClient(
            session, API_ID, API_HASH, proxy=proxy, timeout=self.config.get("PROXY_TIMEOUT", 15),
            device_model=device_params['device_model'], system_version=device_params['system_version']
        )
        client.parse_mode = CustomMarkdown()

        try:
            await client.connect()
            return client
        except Exception as e:
            LOGGER.error(f"Worker connection failed: {e}")
            sentry_sdk.capture_exception(e)
            return None

    # --- NEW: AI Error Analysis ---
    async def _get_ai_error_analysis(self, traceback_str: str) -> str:
        """Uses Gemini to analyze a traceback and suggest a cause and solution."""
        if not GEMINI_API_KEY:
            return "AI analysis disabled: GEMINI_API_KEY not set."

        prompt = (
            "You are an expert Python and Telethon developer. Analyze the following traceback from a Telegram bot. "
            "Provide a brief, one-sentence summary of the root cause. Then, suggest a potential solution in plain English. "
            "Be concise.\n\n"
            f"**Traceback:**\n```\n{traceback_str}\n```"
        )
        
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}

        try:
            async with httpx.AsyncClient(timeout=self.config.get("AI_REQUEST_TIMEOUT", 20)) as client:
                response = await client.post(api_url, json=payload, headers={'Content-Type': 'application/json'})
                response.raise_for_status()
                result = response.json()
                if (candidates := result.get("candidates")) and candidates[0].get("content", {}).get("parts", [{}])[0].get("text"):
                    return candidates[0]["content"]["parts"][0]["text"]
                return "AI analysis returned an unexpected format."
        except Exception as e:
            LOGGER.error(f"AI error analysis failed: {e}")
            return "Could not get analysis from AI due to a network or API error."

    # --- MODIFIED: Enhanced Error Explanation ---
    async def _send_error_explanation(self, user_id: int, e: Exception):
        """Logs an error, gets AI analysis, and sends a detailed report to the admin."""
        LOGGER.error(f"An error occurred for user {user_id}", exc_info=True)
        sentry_sdk.capture_exception(e)
        traceback_str = traceback.format_exc()
        
        # Get AI analysis of the error
        ai_analysis = await self._get_ai_error_analysis(traceback_str)

        # Simplified message for the user
        user_message = "❌ یک خطای پیش‌بینی نشده رخ داد. گزارش به ادمین ارسال شد."
        if isinstance(e, errors.FloodWaitError):
            user_message = f"⏳ تلگرام از ما خواسته است که {e.seconds} ثانیه صبر کنیم. لطفاً بعداً دوباره تلاش کنید."
        elif isinstance(e, (errors.UserDeactivatedBanError, errors.PhoneNumberBannedError)):
            user_message = "🚨 این حساب توسط تلگرام مسدود شده است."
        
        try:
            await self.bot.send_message(user_id, user_message)
        except Exception as send_error:
            LOGGER.error(f"Failed to send error explanation to user {user_id}: {send_error}")

        # Detailed report for the admin
        try:
            admin_report = (
                f"**🚨 Error Report for User `{user_id}`**\n\n"
                f"**🤖 AI Analysis:**\n{ai_analysis}\n\n"
                f"**📄 Full Traceback:**\n```\n{traceback_str}\n```"
            )
            if len(admin_report) > 4096:
                admin_report = admin_report[:4090] + "\n...```" # Truncate if too long
            await self.bot.send_message(ADMIN_USER_ID, admin_report)
        except Exception as admin_send_error:
            LOGGER.error(f"Failed to send full error report to admin: {admin_send_error}")

    # --- Bot Handlers with Menu Integration ---
    @events.register(events.NewMessage(pattern='/start'))
    async def start_handler(self, event):
        user_id = event.sender_id
        user_states[user_id] = 'main_menu'
        await event.respond("🤖 به ربات سازنده گروه خوش آمدید!", buttons=self.get_main_menu())

    @events.register(events.NewMessage)
    async def message_handler(self, event):
        user_id = event.sender_id
        state = user_states.get(user_id)
        text = event.text

        if state == 'main_menu':
            await self.main_menu_router(event)
        elif state == 'awaiting_username':
            user_data.setdefault(user_id, {})['username'] = text
            user_states[user_id] = 'awaiting_password'
            await event.respond("🔒 لطفاً رمز عبور خود را وارد کنید:", buttons=Button.clear())
        elif state == 'awaiting_password':
            await self.handle_login(event)
        else:
            # Default action if state is unknown
            user_states[user_id] = 'main_menu'
            await event.respond("دستور نامشخص است. به منوی اصلی بازگشتید.", buttons=self.get_main_menu())

    async def main_menu_router(self, event):
        text = event.text
        if text == BTN_RESERVE_FOOD:
            user_states[event.sender_id] = 'awaiting_username'
            await event.respond("برای شروع رزرو، لطفاً نام کاربری (شماره دانشجویی) خود را وارد کنید:", buttons=None)
        elif text in [BTN_MANAGE_ACCOUNTS, BTN_JOIN_LINK, BTN_EXPORT_LINKS, BTN_START_MANUAL_CONV, BTN_STOP_MANUAL_CONV, BTN_SET_AI_KEYWORDS, BTN_SET_CONV_ACCOUNTS]:
            await event.respond("این ویژگی در حال توسعه است.", buttons=self.get_main_menu())
        else:
            await event.respond("لطفاً از دکمه‌های زیر استفاده کنید.", buttons=self.get_main_menu())
            
    def get_main_menu(self):
        return self.bot.build_reply_markup([
            [Button.text(BTN_RESERVE_FOOD)],
            [Button.text(BTN_MANAGE_ACCOUNTS), Button.text(BTN_JOIN_LINK)],
            [Button.text(BTN_EXPORT_LINKS)],
            [Button.text(BTN_START_MANUAL_CONV), Button.text(BTN_STOP_MANUAL_CONV)],
            [Button.text(BTN_SET_AI_KEYWORDS), Button.text(BTN_SET_CONV_ACCOUNTS)],
        ])

    async def handle_login(self, event):
        user_id = event.sender_id
        password = event.text
        username = user_data.get(user_id, {}).get('username')

        if not username:
            await event.respond("خطایی رخ داده است. لطفاً با /start مجدداً شروع کنید.")
            user_states[user_id] = 'main_menu'
            await self.send_main_menu(event)
            return

        msg = await event.respond("⏳ در حال ورود به سامانه... لطفاً کمی صبر کنید.")
        
        reservation_system = FoodReservationSystem()
        user_data[user_id]['reservation_system'] = reservation_system
        
        try:
            login_successful = await reservation_system.login(username, password)
            if login_successful:
                reservation_data = await reservation_system.get_reservation_data()
                if reservation_data:
                    user_data[user_id]['reservation_data'] = reservation_data
                    await msg.delete()
                    await self.show_days_menu(event)
                    user_states[user_id] = 'choosing_day'
                else:
                    await msg.edit("❌ ورود موفق بود اما دریافت اطلاعات رزرو با مشکل مواجه شد.")
                    user_states[user_id] = 'main_menu'
                    await self.send_main_menu(event)
            else:
                await msg.edit("❌ نام کاربری یا رمز عبور اشتباه است.")
                user_states[user_id] = 'main_menu'
                await self.send_main_menu(event)
        except Exception as e:
            await msg.delete()
            await self._send_error_explanation(user_id, e)
            user_states[user_id] = 'main_menu'
            await self.send_main_menu(event)

    async def show_days_menu(self, event, edit=False):
        user_id = event.sender_id
        reservation_data = user_data.get(user_id, {}).get('reservation_data')
        
        if not reservation_data:
            await event.respond("اطلاعات رزرو یافت نشد. لطفاً دوباره وارد شوید.")
            user_states[user_id] = 'main_menu'
            return

        keyboard = []
        for day in reservation_data:
            if day.get("DayState") == 0:
                day_title = f'{day.get("DayTitle", "")} - {day.get("DayDate", "")}'
                callback_data = f'day_{day.get("DayDate")}'
                keyboard.append([Button.inline(day_title, data=callback_data.encode())])
        
        keyboard.append([Button.inline("بازگشت به منوی اصلی", data=b"back_to_main")])
        message = "📅 لطفاً روز مورد نظر خود را برای رزرو انتخاب کنید:" if keyboard else "در حال حاضر روز فعالی برای رزرو وجود ندارد."
        
        if edit:
            await event.edit(message, buttons=keyboard)
        else:
            await event.respond(message, buttons=keyboard)

    @events.register(events.CallbackQuery)
    async def callback_query_handler(self, event):
        user_id = event.sender_id
        state = user_states.get(user_id)
        data = event.data.decode('utf-8')

        if data == "back_to_main":
            user_states[user_id] = 'main_menu'
            await event.delete()
            await self.send_main_menu(event)
            return

        if state == 'choosing_day' and data.startswith('day_'):
            await self.handle_day_selection(event)
        elif state == 'reservation_action':
            await self.handle_reservation_action(event)

    async def handle_day_selection(self, event):
        user_id = event.sender_id
        selected_date = event.data.decode('utf-8').split('_')[1]
        user_data[user_id]['selected_date'] = selected_date
        
        reservation_data = user_data[user_id].get('reservation_data')
        day_data = next((d for d in reservation_data if d.get("DayDate") == selected_date), None)
        
        if not day_data:
            await event.edit("خطا: روز انتخاب شده یافت نشد.")
            return

        user_data[user_id]['selected_day_data'] = day_data
        keyboard = []
        
        for meal in day_data.get("Meals", []):
            if meal.get("FoodMenu") and meal.get("MealState") == 0:
                meal_name = meal.get("MealName")
                callback_data = f'meal_{meal.get("MealId")}'
                keyboard.append([Button.inline(f"رزرو {meal_name}", data=callback_data.encode())])

        keyboard.append([Button.inline("🤖 اجازه بده هوش مصنوعی امروز برایت تصمیم بگیرد", data=b"ai_suggest")])
        keyboard.append([Button.inline("⬅️ بازگشت به انتخاب روز", data=b"back_to_days")])
        
        await event.edit(f'شما روز {selected_date} را انتخاب کردید. چه کاری می‌خواهید انجام دهید؟', buttons=keyboard)
        user_states[user_id] = 'reservation_action'

    async def handle_reservation_action(self, event):
        user_id = event.sender_id
        action = event.data.decode('utf-8')

        if action == "back_to_days":
            user_states[user_id] = 'choosing_day'
            await self.show_days_menu(event, edit=True)
            return

        if action == "ai_suggest":
            await event.edit("🧠 در حال مشورت با هوش مصنوعی... لطفاً صبر کنید.", buttons=await event.get_buttons())
            day_data = user_data[user_id].get('selected_day_data')
            recommendation = await get_ai_recommendation(day_data)
            
            current_buttons = await event.get_buttons()
            await event.edit(f"💡 **پیشنهاد هوش مصنوعی:**\n\n{recommendation}", buttons=current_buttons)
            return

        if action.startswith("meal_"):
            selected_meal_id = int(action.split('_')[1])
            day_data = user_data[user_id].get('selected_day_data')
            meal_data = next((m for m in day_data.get("Meals", []) if m.get("MealId") == selected_meal_id), None)
            
            if not meal_data or not meal_data.get("FoodMenu"):
                await event.edit("خطا: این وعده غذایی در دسترس نیست.")
                return

            food_to_reserve = meal_data["FoodMenu"][0]
            self_to_reserve = food_to_reserve["SelfMenu"][0]

            reservation_payload = [{
                "Row": 0, "Id": meal_data["Id"], "Date": day_data["DayDate"],
                "MealId": meal_data["MealId"], "FoodId": food_to_reserve["FoodId"],
                "FoodName": food_to_reserve["FoodName"], "SelfId": self_to_reserve["SelfId"],
                "LastCounts": 0, "Counts": 1, "Price": self_to_reserve.get("Price", 0),
                "SobsidPrice": self_to_reserve.get("Yarane", 0), "PriceType": 2, "State": 0,
                "Type": 1, "OP": 1, "OpCategory": 1, "Provider": 1, "Saved": 0,
                "MealName": meal_data["MealName"], "DayName": day_data["DayTitle"],
                "SelfName": self_to_reserve["SelfName"], "DayIndex": day_data["DayId"],
                "MealIndex": meal_data["MealId"] - 1
            }]

            await event.edit("در حال ثبت رزرو شما...")
            
            reservation_system = user_data[user_id]['reservation_system']
            success, message = await reservation_system.make_reservation(reservation_payload)

            final_message = f"✅ **نتیجه رزرو:**\n{message}" if success else f"❌ **نتیجه رزرو:**\n{message}"
            
            keyboard = [[Button.inline("⬅️ بازگشت به انتخاب روز", data=b"back_to_days")]]
            await event.edit(final_message, buttons=keyboard)
            user_states[user_id] = 'reservation_action'


async def main():
    """Start the bot."""
    if not all([TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, API_ID, API_HASH]):
        LOGGER.critical("FATAL: Missing one or more required environment variables.")
        return
        
    # Register handlers
    bot.add_event_handler(start_handler)
    bot.add_event_handler(cancel_handler)
    bot.add_event_handler(message_handler)
    bot.add_event_handler(callback_query_handler)

    await bot.start(bot_token=TELEGRAM_BOT_TOKEN)
    LOGGER.info("Bot is running...")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    bot.loop.run_until_complete(main())
