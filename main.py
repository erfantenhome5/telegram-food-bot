import logging
import os
import json
import random
from typing import Dict, Any, Tuple, List, Optional
import httpx
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from dotenv import load_dotenv

# --- Load Environment Variables ---
# Make sure you have a .env file in the same directory with your tokens
# Example .env file:
# TELEGRAM_BOT_TOKEN="12345:your_telegram_bot_token"
# GEMINI_API_KEY="your_gemini_api_key"
# API_ID="your_api_id"
# API_HASH="your_api_hash"
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

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

# --- State and Data Storage (In-memory) ---
user_states = {}
user_data = {}

# --- Button Texts (Persian) ---
BTN_MANAGE_ACCOUNTS = "👤 مدیریت حساب‌ها"
BTN_JOIN_LINK = "🔗 عضویت با لینک"
BTN_EXPORT_LINKS = "📤 صدور لینک‌های گروه"
BTN_START_MANUAL_CONV = "💬 شروع مکالمه دستی"
BTN_STOP_MANUAL_CONV = "⏹️ توقف مکالمه دستی"
BTN_SET_AI_KEYWORDS = "📝 تنظیم کلمات کلیدی AI"
BTN_SET_CONV_ACCOUNTS = "🗣️ تنظیم حساب‌های گفتگو"
BTN_ADD_ACCOUNT = "➕ افزودن حساب جدید"
BTN_BACK = "⬅️ بازگشت"
BTN_RESERVE_FOOD = "🍔 رزرو غذا"


# --- Website Interaction Class ---
class FoodReservationSystem:
    """
    Handles all web interactions with the food.gums.ac.ir website.
    """
    BASE_URL = "https://food.gums.ac.ir"
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ]

    def __init__(self):
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": random.choice(self.USER_AGENTS),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.5",
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
            timeout=20.0,
            follow_redirects=True
        )
        self.xsrf_token = None

    async def _get_xsrf_token(self) -> bool:
        """Fetches the initial login page to get the xsrf token."""
        try:
            login_url = f"{self.BASE_URL}/identity/login"
            response = await self.client.get(login_url)
            response.raise_for_status()
            if 'idsrv.xsrf' in response.cookies:
                self.xsrf_token = response.cookies['idsrv.xsrf']
                LOGGER.info("Successfully retrieved XSRF token.")
                return True
            LOGGER.error("Could not find 'idsrv.xsrf' token in login page response.")
            return False
        except httpx.RequestError as e:
            LOGGER.error(f"Error fetching login page to get XSRF token: {e}")
            return False

    async def login(self, username, password) -> bool:
        """Logs into the system and stores session cookies."""
        if not await self._get_xsrf_token():
            return False

        login_payload = {
            "idsrv.xsrf": self.xsrf_token,
            "username": username,
            "password": password,
        }
        login_post_url = f"{self.BASE_URL}/identity/login?signin=e44508c4639494bbd37f8a57cb006f34"

        try:
            response = await self.client.post(
                login_post_url,
                data=login_payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            response.raise_for_status()
            if "/identity/login" not in str(response.url):
                LOGGER.info(f"Login successful for user {username}.")
                return True
            else:
                LOGGER.warning(f"Login failed for user {username}. Final URL was still on login page.")
                return False
        except httpx.HTTPStatusError as e:
            LOGGER.error(f"HTTP error during login for {username}: {e.response.status_code} - {e.response.text}")
            return False
        except httpx.RequestError as e:
            LOGGER.error(f"Network error during login for {username}: {e}")
            return False

    async def get_reservation_data(self) -> Optional[List[Dict]]:
        """Fetches the weekly reservation data."""
        reservation_url = f"{self.BASE_URL}/api/v0/Reservation?lastdate=&navigation=0"
        try:
            response = await self.client.get(reservation_url)
            response.raise_for_status()
            data = response.json()
            LOGGER.info(f"Successfully fetched reservation data. Found {len(data)} days.")
            return data
        except (httpx.RequestError, json.JSONDecodeError, httpx.HTTPStatusError) as e:
            LOGGER.error(f"Failed to get reservation data: {e}")
            return None

    async def make_reservation(self, reservation_payload: List[Dict]) -> Tuple[bool, str]:
        """Submits a reservation request."""
        reservation_post_url = f"{self.BASE_URL}/api/v0/Reservation"
        try:
            response = await self.client.post(
                reservation_post_url,
                json=reservation_payload,
                headers={"Content-Type": "application/json;charset=UTF-8"}
            )
            response.raise_for_status()
            response_data = response.json()
            if response_data and isinstance(response_data, list) and response_data[0].get("StateMessage") == "با موفقیت ثبت شد":
                msg = response_data[0].get("StateMessage", "رزرو با موفقیت انجام شد.")
                LOGGER.info(f"Reservation successful: {msg}")
                return True, msg
            else:
                msg = response_data[0].get("StateMessage", "خطا در ثبت رزرو.")
                LOGGER.warning(f"Reservation failed: {response.text}")
                return False, msg
        except (httpx.RequestError, json.JSONDecodeError, httpx.HTTPStatusError) as e:
            LOGGER.error(f"Failed to make reservation: {e}")
            return False, "خطای شبکه در هنگام ثبت رزرو رخ داد."

# --- AI Helper ---
async def get_ai_recommendation(day_data: Dict[str, Any]) -> str:
    """Gets a meal recommendation from the Gemini API."""
    if not GEMINI_API_KEY:
        return "متاسفانه کلید API هوش مصنوعی تنظیم نشده است."

    meal_options = []
    for meal in day_data.get("Meals", []):
        if meal.get("FoodMenu"):
            meal_name = meal.get("MealName", "وعده غذایی")
            foods = [food.get("FoodName", "نامشخص") for food in meal.get("FoodMenu")]
            meal_options.append(f"{meal_name}: {', '.join(foods)}")

    if not meal_options:
        return "غذایی برای این روز یافت نشد تا پیشنهادی بدهم."

    options_text = "\n".join(meal_options)
    prompt = (
        "شما یک مشاور غذایی خوش ذوق و دوست داشتنی هستید. بر اساس گزینه‌های غذایی زیر برای امروز، "
        "کدام یک را به عنوان خوشمزه‌ترین و بهترین انتخاب پیشنهاد می‌دهید و چرا؟ لطفاً پاسخ خود را به زبان فارسی، "
        "با لحنی دوستانه، جذاب و متقاعدکننده ارائه دهید.\n\n"
        f"گزینه‌های امروز:\n{options_text}"
    )

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(api_url, json=payload, headers={'Content-Type': 'application/json'})
            response.raise_for_status()
            result = response.json()
            if (candidates := result.get("candidates")) and candidates[0].get("content", {}).get("parts", [{}])[0].get("text"):
                return candidates[0]["content"]["parts"][0]["text"]
            else:
                LOGGER.error(f"Unexpected Gemini API response structure: {result}")
                return "پاسخ نامشخصی از هوش مصنوعی دریافت شد."
    except Exception as e:
        LOGGER.error(f"Error calling Gemini API: {e}")
        return "متاسفانه در ارتباط با هوش مصنوعی خطایی رخ داد."

# --- Bot Client Setup ---
bot = TelegramClient('bot_session', int(API_ID), API_HASH)

# --- Menu Functions ---
async def send_main_menu(event):
    """Sends the main menu with reply keyboard."""
    buttons = [
        [Button.text(BTN_RESERVE_FOOD)],
        [Button.text(BTN_MANAGE_ACCOUNTS), Button.text(BTN_JOIN_LINK)],
        [Button.text(BTN_EXPORT_LINKS)],
        [Button.text(BTN_START_MANUAL_CONV), Button.text(BTN_STOP_MANUAL_CONV)],
        [Button.text(BTN_SET_AI_KEYWORDS), Button.text(BTN_SET_CONV_ACCOUNTS)],
    ]
    await event.respond("منوی اصلی:", buttons=buttons)

# --- Bot Handlers ---
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    """Starts the conversation and shows the main menu."""
    user_id = event.sender_id
    user_states[user_id] = 'main_menu'
    await event.respond("🤖 به ربات رزرو غذا خوش آمدید!")
    await send_main_menu(event)

@bot.on(events.NewMessage(pattern='/cancel'))
async def cancel_handler(event):
    """Cancels the current operation and returns to main menu."""
    user_id = event.sender_id
    user_states[user_id] = 'main_menu'
    await event.respond("عملیات لغو شد.")
    await send_main_menu(event)

@bot.on(events.NewMessage)
async def message_handler(event):
    """Handles incoming messages based on user state and button presses."""
    user_id = event.sender_id
    state = user_states.get(user_id)
    text = event.text

    # --- Main Menu Button Handlers ---
    if state == 'main_menu':
        if text == BTN_RESERVE_FOOD:
            user_states[user_id] = 'awaiting_username'
            await event.respond("برای شروع رزرو، لطفاً نام کاربری (شماره دانشجویی) خود را وارد کنید:", buttons=None)
        elif text == BTN_MANAGE_ACCOUNTS:
            await event.respond("این ویژگی (مدیریت حساب‌ها) هنوز پیاده‌سازی نشده است.")
        elif text == BTN_JOIN_LINK:
            await event.respond("این ویژگی (عضویت با لینک) هنوز پیاده‌سازی نشده است.")
        # ... Add handlers for other main menu buttons here
        else:
            # Fallback for unexpected text
            await send_main_menu(event)
        return

    # --- Conversation Flow Handlers ---
    if state == 'awaiting_username':
        user_data[user_id] = {'username': text}
        user_states[user_id] = 'awaiting_password'
        await event.respond("🔒 لطفاً رمز عبور خود را وارد کنید:")
    
    elif state == 'awaiting_password':
        await handle_login(event)

async def handle_login(event):
    """Handles the login process."""
    user_id = event.sender_id
    password = event.text
    username = user_data.get(user_id, {}).get('username')

    if not username:
        await event.respond("خطایی رخ داده است. لطفاً با /start مجدداً شروع کنید.")
        user_states[user_id] = 'main_menu'
        return

    msg = await event.respond("⏳ در حال ورود به سامانه... لطفاً کمی صبر کنید.")
    
    reservation_system = FoodReservationSystem()
    user_data[user_id]['reservation_system'] = reservation_system
    
    login_successful = await reservation_system.login(username, password)

    if login_successful:
        reservation_data = await reservation_system.get_reservation_data()
        if reservation_data:
            user_data[user_id]['reservation_data'] = reservation_data
            await msg.delete()
            await show_days_menu(event)
            user_states[user_id] = 'choosing_day'
        else:
            await msg.edit("❌ ورود موفق بود اما دریافت اطلاعات رزرو با مشکل مواجه شد.")
            user_states[user_id] = 'main_menu'
            await send_main_menu(event)
    else:
        await msg.edit("❌ نام کاربری یا رمز عبور اشتباه است.")
        user_states[user_id] = 'main_menu'
        await send_main_menu(event)

async def show_days_menu(event, edit=False):
    """Displays the available days for reservation."""
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

@bot.on(events.CallbackQuery)
async def callback_query_handler(event):
    """Handles all inline button presses."""
    user_id = event.sender_id
    state = user_states.get(user_id)
    data = event.data.decode('utf-8')

    if data == "back_to_main":
        user_states[user_id] = 'main_menu'
        await event.delete()
        await send_main_menu(event)
        return

    if state == 'choosing_day' and data.startswith('day_'):
        await handle_day_selection(event)
    elif state == 'reservation_action':
        await handle_reservation_action(event)

async def handle_day_selection(event):
    """Handles user's day selection and shows meal options."""
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

async def handle_reservation_action(event):
    """Handles the user's action for a specific day."""
    user_id = event.sender_id
    action = event.data.decode('utf-8')

    if action == "back_to_days":
        user_states[user_id] = 'choosing_day'
        await show_days_menu(event, edit=True)
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
        
    await bot.start(bot_token=TELEGRAM_BOT_TOKEN)
    LOGGER.info("Bot is running...")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    bot.loop.run_until_complete(main())
