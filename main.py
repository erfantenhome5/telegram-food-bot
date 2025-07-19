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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", OPENROUTER_API_KEY)


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

# --- Website Interaction Class ---
class FoodReservationSystem:
    BASE_URL = "https://food.gums.ac.ir"
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ]

    def __init__(self):
        self.client = httpx.AsyncClient(
            headers={"User-Agent": random.choice(self.USER_AGENTS)},
            timeout=20.0, follow_redirects=True
        )
        self.xsrf_token = None

    async def _get_xsrf_token(self) -> bool:
        try:
            response = await self.client.get(f"{self.BASE_URL}/identity/login")
            response.raise_for_status()
            if 'idsrv.xsrf' in response.cookies:
                self.xsrf_token = response.cookies['idsrv.xsrf']
                return True
            return False
        except httpx.RequestError as e:
            LOGGER.error(f"XSRF token fetch error: {e}")
            raise ConnectionError("Could not connect to the food service to get a security token.")

    async def login(self, username, password) -> bool:
        if not await self._get_xsrf_token():
            return False
        payload = {"idsrv.xsrf": self.xsrf_token, "username": username, "password": password}
        try:
            response = await self.client.post(
                f"{self.BASE_URL}/identity/login", data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            response.raise_for_status()
            return "/identity/login" not in str(response.url)
        except httpx.RequestError as e:
            LOGGER.error(f"Login network error for {username}: {e}")
            raise ConnectionError("A network error occurred while trying to log in.")

    async def get_reservation_data(self) -> Optional[List[Dict]]:
        try:
            response = await self.client.get(f"{self.BASE_URL}/api/v0/Reservation?lastdate=&navigation=0")
            response.raise_for_status()
            return response.json()
        except (httpx.RequestError, json.JSONDecodeError) as e:
            LOGGER.error(f"Failed to get reservation data: {e}")
            raise ConnectionError("Could not fetch reservation data from the server.")

    async def make_reservation(self, payload: List[Dict]) -> Tuple[bool, str]:
        try:
            response = await self.client.post(f"{self.BASE_URL}/api/v0/Reservation", json=payload)
            response.raise_for_status()
            data = response.json()
            if data and data[0].get("StateMessage") == "با موفقیت ثبت شد":
                return True, data[0].get("StateMessage")
            return False, data[0].get("StateMessage", "An unknown error occurred.")
        except (httpx.RequestError, json.JSONDecodeError) as e:
            LOGGER.error(f"Failed to make reservation: {e}")
            raise ConnectionError("A network error occurred while making the reservation.")

# --- Centralized Configuration ---
class Config:
    BTN_RESERVE_FOOD = "🍔 رزرو غذا"
    BTN_MANAGE_ACCOUNTS = "👤 مدیریت حساب‌ها"
    BTN_JOIN_VIA_LINK = "🔗 عضویت با لینک"
    BTN_EXPORT_LINKS = "📤 صدور لینک‌های گروه"
    BTN_START_MANUAL_CONV = "💬 شروع مکالمه دستی"
    BTN_STOP_MANUAL_CONV = "⏹️ توقف مکالمه دستی"
    BTN_SET_AI_KEYWORDS = "📝 تنظیم کلمات کلیدی AI"
    BTN_SET_CONV_ACCOUNTS = "🗣️ تنظیم حساب‌های گفتگو"
    BTN_BACK_TO_MAIN = "⬅️ بازگشت به منوی اصلی"

class GroupCreatorBot:
    def __init__(self):
        self.bot = TelegramClient(StringSession(), API_ID, API_HASH)
        self.user_states: Dict[int, str] = {}
        self.user_data: Dict[int, Dict[str, Any]] = {}

    def get_main_menu(self):
        return self.bot.build_reply_markup([
            [Button.text(Config.BTN_RESERVE_FOOD)],
            [Button.text(Config.BTN_MANAGE_ACCOUNTS), Button.text(Config.BTN_JOIN_VIA_LINK)],
            [Button.text(Config.BTN_EXPORT_LINKS)],
            [Button.text(Config.BTN_START_MANUAL_CONV), Button.text(Config.BTN_STOP_MANUAL_CONV)],
            [Button.text(Config.BTN_SET_AI_KEYWORDS), Button.text(Config.BTN_SET_CONV_ACCOUNTS)],
        ])

    async def get_ai_recommendation(self, day_data: Dict[str, Any]) -> str:
        if not GEMINI_API_KEY: return "سرویس هوش مصنوعی پیکربندی نشده است."
        meal_options = [f"{m['MealName']}: {', '.join(f['FoodName'] for f in m['FoodMenu'])}" for m in day_data.get("Meals", []) if m.get("FoodMenu")]
        if not meal_options: return "غذایی برای پیشنهاد یافت نشد."
        
        prompt = f"شما یک مشاور غذایی خوش ذوق هستید. از بین این گزینه‌ها بهترین را پیشنهاد دهید و دلیل خود را به صورت دوستانه بگویید:\n\n{chr(10).join(meal_options)}"
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(api_url, json=payload)
                response.raise_for_status()
                return response.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            LOGGER.error(f"AI recommendation error: {e}")
            return "خطا در ارتباط با سرویس هوش مصنوعی."

    async def send_error_explanation(self, event, e: Exception):
        user_id = event.sender_id
        traceback_str = traceback.format_exc()
        LOGGER.error(f"Error for user {user_id}: {traceback_str}")
        
        # AI Analysis
        ai_analysis = await self.get_ai_error_analysis(traceback_str)

        # User-friendly message
        user_message = "❌ یک خطای پیش‌بینی نشده رخ داد. گزارش به ادمین ارسال شد."
        if isinstance(e, ConnectionError):
            user_message = f"🔌 خطای اتصال: {e}"
        elif isinstance(e, errors.FloodWaitError):
            user_message = f"⏳ تلگرام از ما خواسته {e.seconds} ثانیه صبر کنیم. لطفاً بعداً دوباره تلاش کنید."

        await event.respond(user_message)
        
        # Admin report
        admin_report = f"**🚨 Error for user `{user_id}`**\n\n**🤖 AI Analysis:**\n{ai_analysis}\n\n**Traceback:**\n```{traceback_str[:3500]}```"
        await self.bot.send_message(ADMIN_USER_ID, admin_report)

    async def get_ai_error_analysis(self, traceback_str: str) -> str:
        if not GEMINI_API_KEY: return "AI analysis disabled."
        prompt = f"Analyze this Python traceback and provide a concise, one-sentence explanation of the root cause and a suggested fix.\n\nTraceback:\n{traceback_str}"
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(api_url, json=payload)
                response.raise_for_status()
                return response.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            return f"AI analysis failed: {e}"

    def register_handlers(self):
        self.bot.add_event_handler(self.start_handler, events.NewMessage(pattern='/start'))
        self.bot.add_event_handler(self.message_handler, events.NewMessage)
        self.bot.add_event_handler(self.callback_query_handler, events.CallbackQuery)

    async def start_handler(self, event):
        self.user_states[event.sender_id] = 'main_menu'
        await event.respond("🤖 به ربات جامع مدیریت و رزرو خوش آمدید!", buttons=self.get_main_menu())

    async def message_handler(self, event):
        user_id = event.sender_id
        state = self.user_states.get(user_id)
        text = event.text

        if text == Config.BTN_BACK_TO_MAIN:
            self.user_states[user_id] = 'main_menu'
            await event.respond("به منوی اصلی بازگشتید.", buttons=self.get_main_menu())
            return

        if state == 'main_menu':
            if text == Config.BTN_RESERVE_FOOD:
                self.user_states[user_id] = 'awaiting_username'
                await event.respond("لطفاً نام کاربری خود را وارد کنید:", buttons=None)
            else:
                await event.respond("این ویژگی در حال توسعه است.")
        elif state == 'awaiting_username':
            self.user_data.setdefault(user_id, {})['username'] = text
            self.user_states[user_id] = 'awaiting_password'
            await event.respond("🔒 لطفاً رمز عبور خود را وارد کنید:")
        elif state == 'awaiting_password':
            await self.handle_login(event)

    async def handle_login(self, event):
        user_id = event.sender_id
        msg = await event.respond("⏳ در حال ورود به سامانه...")
        try:
            system = FoodReservationSystem()
            self.user_data[user_id]['system'] = system
            login_ok = await system.login(self.user_data[user_id]['username'], event.text)
            if login_ok:
                res_data = await system.get_reservation_data()
                if res_data:
                    self.user_data[user_id]['reservation_data'] = res_data
                    await msg.delete()
                    await self.show_days_menu(event)
                    self.user_states[user_id] = 'choosing_day'
                else:
                    await msg.edit("❌ ورود موفق بود اما دریافت اطلاعات رزرو ناموفق بود.")
                    self.user_states[user_id] = 'main_menu'
            else:
                await msg.edit("❌ نام کاربری یا رمز عبور اشتباه است.")
                self.user_states[user_id] = 'main_menu'
        except Exception as e:
            await msg.delete()
            await self.send_error_explanation(event, e)
            self.user_states[user_id] = 'main_menu'

    async def show_days_menu(self, event, edit=False):
        user_id = event.sender_id
        res_data = self.user_data.get(user_id, {}).get('reservation_data', [])
        buttons = [[Button.inline(f'{d["DayTitle"]} - {d["DayDate"]}', data=f'day_{d["DayDate"]}')] for d in res_data if d.get("DayState") == 0]
        buttons.append([Button.inline(Config.BTN_BACK_TO_MAIN, data=b'back_to_main')])
        message = "📅 روز مورد نظر را انتخاب کنید:" if len(buttons) > 1 else "روز فعالی برای رزرو یافت نشد."
        
        if edit: await event.edit(message, buttons=buttons)
        else: await event.respond(message, buttons=buttons)

    async def callback_query_handler(self, event):
        user_id = event.sender_id
        state = self.user_states.get(user_id)
        data_str = event.data.decode('utf-8')

        if data_str == 'back_to_main':
            self.user_states[user_id] = 'main_menu'
            await event.delete()
            await self.bot.send_message(user_id, "به منوی اصلی بازگشتید.", buttons=self.get_main_menu())
            return
        
        if state == 'choosing_day' and data_str.startswith('day_'):
            await self.handle_day_selection(event)
        elif state == 'reservation_action':
            await self.handle_reservation_action(event)

    async def handle_day_selection(self, event):
        user_id = event.sender_id
        date = event.data.decode('utf-8').split('_')[1]
        self.user_data[user_id]['selected_date'] = date
        day_data = next((d for d in self.user_data[user_id]['reservation_data'] if d['DayDate'] == date), None)
        if not day_data:
            await event.answer("روز یافت نشد!", alert=True)
            return

        self.user_data[user_id]['selected_day_data'] = day_data
        buttons = [[Button.inline(f"رزرو {m['MealName']}", data=f'meal_{m["MealId"]}')] for m in day_data.get("Meals", []) if m.get("FoodMenu") and m.get("MealState") == 0]
        buttons.append([Button.inline("🤖 پیشنهاد هوش مصنوعی", data=b"ai_suggest")])
        buttons.append([Button.inline("⬅️ بازگشت به روزها", data=b"back_to_days")])
        
        await event.edit(f'انتخاب برای روز {date}:', buttons=buttons)
        self.user_states[user_id] = 'reservation_action'

    async def handle_reservation_action(self, event):
        user_id = event.sender_id
        action = event.data.decode('utf-8')

        if action == "back_to_days":
            self.user_states[user_id] = 'choosing_day'
            await self.show_days_menu(event, edit=True)
            return

        if action == "ai_suggest":
            await event.answer("🧠 در حال پردازش...")
            day_data = self.user_data[user_id].get('selected_day_data')
            recommendation = await self.get_ai_recommendation(day_data)
            await event.edit(f"💡 **پیشنهاد هوش مصنوعی:**\n\n{recommendation}", buttons=await event.get_buttons())
            return

        if action.startswith("meal_"):
            await event.answer("⏳ در حال ثبت رزرو...")
            try:
                meal_id = int(action.split('_')[1])
                day_data = self.user_data[user_id]['selected_day_data']
                meal_data = next((m for m in day_data['Meals'] if m['MealId'] == meal_id), None)
                food = meal_data['FoodMenu'][0]
                self_data = food['SelfMenu'][0]

                payload = [{
                    "Date": day_data["DayDate"], "MealId": meal_id, "FoodId": food["FoodId"],
                    "SelfId": self_data["SelfId"], "Counts": 1, "Price": self_data.get("Price", 0),
                    "State": 0, "Type": 1, "OP": 1, # Default values from observation
                    # ... other necessary fields can be added here if required
                }]
                
                system = self.user_data[user_id]['system']
                success, message = await system.make_reservation(payload)
                
                final_message = f"✅ {message}" if success else f"❌ {message}"
                await event.edit(final_message, buttons=[[Button.inline("⬅️ بازگشت به روزها", data=b"back_to_days")]])
            except Exception as e:
                await event.delete()
                await self.send_error_explanation(event, e)
                self.user_states[user_id] = 'main_menu'

async def main():
    bot_instance = GroupCreatorBot()
    bot_instance.register_handlers()
    await bot_instance.bot.start(bot_token=BOT_TOKEN)
    LOGGER.info("Bot is running...")
    await bot_instance.bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
