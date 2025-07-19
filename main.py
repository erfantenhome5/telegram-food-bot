import logging
import os
import json
from typing import Dict, Any, Tuple, List
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from dotenv import load_dotenv

# --- Load Environment Variables ---
# Make sure you have a .env file in the same directory with your tokens
# Example .env file:
# TELEGRAM_BOT_TOKEN="12345:your_telegram_bot_token"
# GEMINI_API_KEY="your_gemini_api_key"
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

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

# --- Conversation States ---
(
    AWAITING_USERNAME,
    AWAITING_PASSWORD,
    MENU,
    CHOOSING_DAY,
    RESERVATION_ACTION,
) = range(5)

# --- Website Interaction Class ---
class FoodReservationSystem:
    """
    Handles all web interactions with the food.gums.ac.ir website.
    """
    BASE_URL = "https://food.gums.ac.ir"

    def __init__(self):
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
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
            # Extract the xsrf token from cookies
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

            # Successful login usually results in a redirect.
            # httpx handles redirects automatically, so we check the final URL.
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
            
            # Check the response for success message
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
    """
    Gets a meal recommendation from the Gemini API.
    """
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


# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation and asks for username."""
    await update.message.reply_text(
        "🤖 سلام! به ربات رزرو غذای دانشگاه خوش آمدید.\n\n"
        "برای شروع، لطفاً نام کاربری (شماره دانشجویی) خود را وارد کنید:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AWAITING_USERNAME

async def get_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores username and asks for password."""
    context.user_data['username'] = update.message.text
    await update.message.reply_text("🔒 لطفاً رمز عبور خود را وارد کنید:")
    return AWAITING_PASSWORD

async def get_password_and_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores password, attempts login, and shows the main menu."""
    password = update.message.text
    username = context.user_data.get('username')
    
    if not username:
        await update.message.reply_text("خطایی رخ داده است. لطفاً با /start مجدداً شروع کنید.")
        return ConversationHandler.END

    await update.message.reply_text("⏳ در حال ورود به سامانه... لطفاً کمی صبر کنید.")

    # Initialize a new reservation system instance for the user's session
    reservation_system = FoodReservationSystem()
    context.user_data['reservation_system'] = reservation_system
    
    login_successful = await reservation_system.login(username, password)

    if login_successful:
        reservation_data = await reservation_system.get_reservation_data()
        if reservation_data:
            context.user_data['reservation_data'] = reservation_data
            await show_days_menu(update, context)
            return CHOOSING_DAY
        else:
            await update.message.reply_text("❌ ورود موفق بود اما دریافت اطلاعات رزرو با مشکل مواجه شد. لطفاً بعداً دوباره تلاش کنید.")
            return ConversationHandler.END
    else:
        await update.message.reply_text(
            "❌ نام کاربری یا رمز عبور اشتباه است. لطفاً با /start دوباره تلاش کنید."
        )
        return ConversationHandler.END

async def show_days_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the available days for reservation."""
    reservation_data = context.user_data.get('reservation_data')
    if not reservation_data:
        await update.callback_query.message.reply_text("اطلاعات رزرو یافت نشد. لطفاً دوباره وارد شوید.")
        return ConversationHandler.END

    keyboard = []
    for day in reservation_data:
        # We only show days that are active for reservation
        if day.get("DayState") == 0:
            day_title = f'{day.get("DayTitle", "")} - {day.get("DayDate", "")}'
            callback_data = f'day_{day.get("DayDate")}'
            keyboard.append([InlineKeyboardButton(day_title, callback_data=callback_data)])
    
    if not keyboard:
         message = "در حال حاضر روز فعالی برای رزرو وجود ندارد."
    else:
        message = "📅 لطفاً روز مورد نظر خود را برای رزرو انتخاب کنید:"

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.message.edit_text(message, reply_markup=reply_markup)
    else:
        await update.message.reply_text(message, reply_markup=reply_markup)


async def day_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles user's day selection and shows meal options."""
    query = update.callback_query
    await query.answer()
    
    selected_date = query.data.split('_')[1]
    context.user_data['selected_date'] = selected_date
    
    reservation_data = context.user_data.get('reservation_data')
    day_data = next((day for day in reservation_data if day.get("DayDate") == selected_date), None)
    
    if not day_data:
        await query.edit_message_text("خطا: روز انتخاب شده یافت نشد.")
        return CHOOSING_DAY

    context.user_data['selected_day_data'] = day_data
    keyboard = []
    
    for meal in day_data.get("Meals", []):
        # Only show meals that have a food menu and are active
        if meal.get("FoodMenu") and meal.get("MealState") == 0:
            meal_name = meal.get("MealName")
            callback_data = f'meal_{meal.get("MealId")}'
            keyboard.append([InlineKeyboardButton(f"رزرو {meal_name}", callback_data=callback_data)])

    # Add the AI suggestion button
    keyboard.append([InlineKeyboardButton("🤖 اجازه بده هوش مصنوعی امروز برایت تصمیم بگیرد", callback_data="ai_suggest")])
    keyboard.append([InlineKeyboardButton("⬅️ بازگشت به انتخاب روز", callback_data="back_to_days")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(f'شما روز {selected_date} را انتخاب کردید. چه کاری می‌خواهید انجام دهید؟', reply_markup=reply_markup)
    
    return RESERVATION_ACTION


async def reservation_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user's action for a specific day (reserve meal or ask AI)."""
    query = update.callback_query
    await query.answer()
    
    action = query.data

    if action == "back_to_days":
        await show_days_menu(update, context)
        return CHOOSING_DAY

    if action == "ai_suggest":
        await query.edit_message_text("🧠 در حال مشورت با هوش مصنوعی... لطفاً صبر کنید.")
        day_data = context.user_data.get('selected_day_data')
        recommendation = await get_ai_recommendation(day_data)
        
        # After showing the recommendation, show the menu again
        keyboard = query.message.reply_markup.inline_keyboard
        await query.edit_message_text(f"💡 **پیشنهاد هوش مصنوعی:**\n\n{recommendation}", reply_markup=InlineKeyboardMarkup(keyboard))
        return RESERVATION_ACTION # Stay in the same state

    if action.startswith("meal_"):
        selected_meal_id = int(action.split('_')[1])
        day_data = context.user_data.get('selected_day_data')
        
        meal_data = next((m for m in day_data.get("Meals", []) if m.get("MealId") == selected_meal_id), None)
        
        if not meal_data or not meal_data.get("FoodMenu"):
            await query.edit_message_text("خطا: این وعده غذایی در دسترس نیست.")
            return RESERVATION_ACTION
            
        # For simplicity, we reserve the first available food item in the menu.
        # A more complex bot could show a food selection menu here.
        food_to_reserve = meal_data["FoodMenu"][0]
        self_to_reserve = food_to_reserve["SelfMenu"][0]

        # Construct the payload based on HAR file analysis
        reservation_payload = [{
            "Row": 0, # This can be a placeholder
            "Id": meal_data["Id"],
            "Date": day_data["DayDate"],
            "MealId": meal_data["MealId"],
            "FoodId": food_to_reserve["FoodId"],
            "FoodName": food_to_reserve["FoodName"],
            "SelfId": self_to_reserve["SelfId"],
            "LastCounts": 0,
            "Counts": 1,
            "Price": self_to_reserve.get("Price", 0),
            "SobsidPrice": self_to_reserve.get("Yarane", 0),
            "PriceType": 2, # Assuming type 2 from HAR
            "State": 0,
            "Type": 1,
            "OP": 1,
            "OpCategory": 1,
            "Provider": 1,
            "Saved": 0,
            "MealName": meal_data["MealName"],
            "DayName": day_data["DayTitle"],
            "SelfName": self_to_reserve["SelfName"],
            "DayIndex": day_data["DayId"],
            "MealIndex": meal_data["MealId"] -1 # Adjusting index
        }]

        await query.edit_message_text("در حال ثبت رزرو شما...")
        
        reservation_system = context.user_data['reservation_system']
        success, message = await reservation_system.make_reservation(reservation_payload)

        final_message = f"✅ **نتیجه رزرو:**\n{message}" if success else f"❌ **نتیجه رزرو:**\n{message}"
        
        keyboard = [[InlineKeyboardButton("⬅️ بازگشت به انتخاب روز", callback_data="back_to_days")]]
        await query.edit_message_text(final_message, reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSING_DAY


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    await update.message.reply_text(
        "عملیات لغو شد. برای شروع مجدد /start را بزنید.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


def main() -> None:
    """Run the bot."""
    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
        LOGGER.critical("FATAL: TELEGRAM_BOT_TOKEN or GEMINI_API_KEY is not set in the environment.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AWAITING_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_username)],
            AWAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_password_and_login)],
            CHOOSING_DAY: [CallbackQueryHandler(day_selection_handler, pattern="^day_")],
            RESERVATION_ACTION: [CallbackQueryHandler(reservation_action_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)

    LOGGER.info("Bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
