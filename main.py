#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Enhanced Telegram Food Reservation Bot with Review System (AI Fallback commented out for debugging)
A bot that helps users login to their food reservation account,
view available reservations, make reservations,
and leave reviews for food items.
"""

import logging
import asyncio
import json
import os
import re
import signal
import sys
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Any
from urllib.parse import unquote

import aiohttp
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
LOGIN_USERNAME, LOGIN_PASSWORD, RESERVATION_SELECTION, REVIEW_RATING, REVIEW_COMMENT = range(5)

# Persian text constants
PERSIAN_TEXT = {
    'welcome': '🍽️ به ربات رزرو غذا خوش آمدید!\n\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:',
    'login_prompt': '🔐 لطفاً نام کاربری خود را وارد کنید:',
    'password_prompt': '🔑 لطفاً رمز عبور خود را وارد کنید:',
    'login_success': '✅ ورود موفقیت‌آمیز بود!',
    'login_failed': '❌ ورود ناموفق! لطفاً نام کاربری و رمز عبور را بررسی کنید.',
    'reservations_title': '📋 رزروهای موجود:',
    'no_reservations': '❌ هیچ رزروی موجود نیست.',
    'select_reservation': '👆 لطفاً رزرو مورد نظر خود را انتخاب کنید:',
    'reservation_success': '✅ رزرو با موفقیت انجام شد!\n\n💭 آیا می‌خواهید نظر خود را درباره این غذا ثبت کنید؟',
    'reservation_failed': '❌ رزرو ناموفق بود. لطفاً دوباره تلاش کنید.',
    'cancel': '❌ لغو',
    'back': '🔙 بازگشت',
    'login': '🔐 ورود',
    'view_reservations': '📋 مشاهده رزروها',
    'my_reviews': '📝 نظرات من',
    'help': '❓ راهنما',
    'error': '❌ خطایی رخ داد. لطفاً دوباره تلاش کنید.',
    'processing': '⏳ در حال پردازش...',
    'food_details': '🍽️ جزئیات غذا:',
    'confirm_reservation': '✅ تأیید رزرو',
    'leave_review': '📝 ثبت نظر',
    'skip_review': '⏭️ رد کردن',
    'rating_prompt': '⭐ لطفاً امتیاز خود را از ۱ تا ۵ انتخاب کنید:',
    'comment_prompt': '💭 لطفاً نظر خود را درباره این غذا بنویسید:\n(برای رد کردن /skip تایپ کنید)',
    'review_saved': '✅ نظر شما با موفقیت ثبت شد! از شما متشکریم.',
    'view_reviews': '👀 مشاهده نظرات',
    'no_reviews': '📝 هنوز نظری ثبت نشده است.',
    'your_reviews_title': '📝 نظرات شما:'
}

class ReviewDatabase:
    """Database manager for storing and retrieving reviews"""
    def __init__(self, db_path: str = "reviews.db"):
        self.db_path = db_path
        self.init_database()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def init_database(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    user_first_name TEXT NOT NULL, food_id TEXT NOT NULL,
                    food_name TEXT NOT NULL, rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                    comment TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_food_id ON reviews(food_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_id ON reviews(user_id)')

    def add_review(self, user_id: int, user_first_name: str, food_id: str,
                   food_name: str, rating: int, comment: Optional[str] = None) -> bool:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO reviews (user_id, user_first_name, food_id, food_name, rating, comment)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (user_id, user_first_name, food_id, food_name, rating, comment))
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adding review: {e}")
            return False

    def get_food_reviews(self, food_id: str) -> List[Dict]:
        try:
            with self._get_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT user_first_name, rating, comment, created_at FROM reviews
                    WHERE food_id = ? ORDER BY created_at DESC
                ''', (food_id,))
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting food reviews: {e}")
            return []

    def get_user_reviews(self, user_id: int) -> List[Dict]:
        try:
            with self._get_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT food_name, rating, comment, created_at FROM reviews
                    WHERE user_id = ? ORDER BY created_at DESC
                ''', (user_id,))
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting user reviews: {e}")
            return []

    def get_food_stats(self, food_id: str) -> Dict:
        stats = {'average_rating': 0, 'total_reviews': 0}
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT AVG(rating), COUNT(*) FROM reviews WHERE food_id = ?', (food_id,))
                result = cursor.fetchone()
                if result and result[0] is not None:
                    stats['average_rating'] = round(result[0], 1)
                    stats['total_reviews'] = result[1]
        except sqlite3.Error as e:
            logger.error(f"Error getting food stats: {e}")
        return stats

class FoodReservationAPI:
    """
    REWRITTEN: API client for food reservation system based on HAR file analysis.
    Handles the complex OIDC authentication flow and uses correct API endpoints.
    """
    def __init__(self):
        self.base_url = "https://food.gums.ac.ir"
        self.session: Optional[aiohttp.ClientSession] = None
        self.xsrf_token: Optional[str] = None
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
        }

    async def _create_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(headers=self.headers, cookie_jar=aiohttp.CookieJar())

    async def close_session(self, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("Aiohttp session closed.")
            self.session = None

    async def login(self, username: str, password: str) -> bool:
        await self._create_session()
        assert self.session is not None
        try:
            # Step 1: Get login page to extract signin URL and initial XSRF token
            login_page_url = f"{self.base_url}/identity/login"
            async with self.session.get(login_page_url) as response:
                if response.status != 200:
                    logger.error(f"Failed to get login page, status: {response.status}")
                    return False
                login_page_html = await response.text()
                signin_match = re.search(r'action="/identity/login\?signin=([^"]+)"', login_page_html)
                idsrv_xsrf_match = re.search(r'name="idsrv\.xsrf" type="hidden" value="([^"]+)"', login_page_html)

                if not signin_match or not idsrv_xsrf_match:
                    logger.error("Could not find signin URL or idsrv.xsrf token on login page.")
                    return False
                signin_value = signin_match.group(1)
                idsrv_xsrf_token = idsrv_xsrf_match.group(1)
            
            # Step 2: POST credentials to log in
            login_post_url = f"{self.base_url}/identity/login?signin={signin_value}"
            login_data = {
                'idsrv.xsrf': idsrv_xsrf_token,
                'username': username,
                'password': password
            }
            async with self.session.post(login_post_url, data=login_data, allow_redirects=False) as response:
                if response.status != 302:
                    logger.error(f"Login POST failed, status: {response.status}. Incorrect credentials?")
                    return False
                redirect_location = response.headers.get('Location')

            # Step 3: Follow the authorization redirect
            async with self.session.get(redirect_location, allow_redirects=False) as response:
                auth_html = await response.text()
                form_action_match = re.search(r'<form method="post" action="([^"]+)">', auth_html)
                if not form_action_match:
                    logger.error("Could not find form action on auth page.")
                    return False
                
                final_post_url = form_action_match.group(1)
                tokens = {m.group(1): m.group(2) for m in re.finditer(r'name="([^"]+)" value="([^"]+)"', auth_html)}
            
            # Step 4: POST the tokens to complete the login and get final auth cookies
            async with self.session.post(final_post_url, data=tokens) as response:
                if response.status != 200:
                    logger.error(f"Final auth POST failed, status: {response.status}")
                    return False
                main_page_html = await response.text()
                # Extract the X-XSRF-Token for API calls
                xsrf_api_token_match = re.search(r'value="(.*?)" id="XSRF-TOKEN"', main_page_html)
                if not xsrf_api_token_match:
                    logger.error("Could not find X-XSRF-TOKEN on main page after login.")
                    return False
                self.xsrf_token = unquote(xsrf_api_token_match.group(1))
            
            logger.info(f"Login successful for user: {username}")
            return True

        except Exception as e:
            logger.error(f"An unexpected error occurred during login: {e}", exc_info=True)
            return False

    async def get_reservations(self) -> List[Dict]:
        if not self.session or not self.xsrf_token:
            logger.warning("Not logged in, can't get reservations.")
            return []
        
        url = f"{self.base_url}/api/v0/Reservation?lastdate=&navigation=0"
        headers = {**self.headers, 'X-XSRF-Token': self.xsrf_token}
        try:
            async with self.session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    # The actual data is nested inside a complex structure
                    all_days_data = []
                    for day_data in data:
                        for meal in day_data.get("Meals", []):
                            for food in meal.get("FoodMenu", []):
                                for self_menu in food.get("SelfMenu", []):
                                    all_days_data.append({
                                        'id': f'{meal["Id"]}_{food["FoodId"]}_{self_menu["SelfId"]}',
                                        'name': food['FoodName'],
                                        'date': day_data['DayDate'],
                                        'time': meal['MealName'],
                                        'price': self_menu.get('Price', 0),
                                        'raw': {**food, **meal, **self_menu, 'Date': day_data['DayDate']}
                                    })
                    return all_days_data
                else:
                    logger.error(f"Failed to get reservations, status: {response.status}")
                    return []
        except Exception as e:
            logger.error(f"Error getting reservations: {e}", exc_info=True)
            return []

    async def make_reservation(self, reservation_raw_data: Dict) -> bool:
        if not self.session or not self.xsrf_token:
            return False
            
        url = f"{self.base_url}/api/v0/Reservation"
        headers = {**self.headers, 'X-XSRF-Token': self.xsrf_token, 'Content-Type': 'application/json;charset=UTF-8'}
        
        # Construct the complex payload based on HAR file analysis
        payload = [{
            "Row": reservation_raw_data.get("Row", 0), # This might need adjustment
            "Id": reservation_raw_data.get("Id"),
            "Date": reservation_raw_data.get("Date"),
            "MealId": reservation_raw_data.get("MealId"),
            "FoodId": reservation_raw_data.get("FoodId"),
            "FoodName": reservation_raw_data.get("FoodName"),
            "SelfId": reservation_raw_data.get("SelfId"),
            "LastCounts": 0,
            "Counts": 1,
            "Price": reservation_raw_data.get("Price"),
            "SobsidPrice": reservation_raw_data.get("Yarane", 0),
            "PriceType": 2, # Assuming type 2 from HAR
            "State": 0,
            "Type": 1,
            "OP": 1,
            "OpCategory": 1,
            "Provider": 1,
            "Saved": 0,
            "MealName": reservation_raw_data.get("MealName"),
            "DayName": reservation_raw_data.get("DayName"),
            "SelfName": reservation_raw_data.get("SelfName"),
            "DayIndex": reservation_raw_data.get("DayIndex", 0),
            "MealIndex": reservation_raw_data.get("MealIndex", 0),
        }]
        
        try:
            async with self.session.post(url, headers=headers, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    # Check for success message in the response
                    if result and result[0].get("StateMessage") == "با موفقیت ثبت شد":
                        return True
                logger.error(f"Reservation failed. Status: {response.status}, Response: {await response.text()}")
                return False
        except Exception as e:
            logger.error(f"Error making reservation: {e}", exc_info=True)
            return False

class EnhancedFoodReservationBot:
    """Enhanced bot class with review system"""
    def __init__(self, token: str):
        self.token = token
        self.api_client = FoodReservationAPI()
        self.review_db = ReviewDatabase()
        self.user_sessions = {}

    def get_main_keyboard(self) -> InlineKeyboardMarkup:
        keyboard = [
            [InlineKeyboardButton(PERSIAN_TEXT['login'], callback_data='login')],
            [InlineKeyboardButton(PERSIAN_TEXT['view_reservations'], callback_data='view_reservations')],
            [InlineKeyboardButton(PERSIAN_TEXT['my_reviews'], callback_data='my_reviews')],
            [InlineKeyboardButton(PERSIAN_TEXT['help'], callback_data='help')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_back_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(PERSIAN_TEXT['back'], callback_data='back')]])

    def get_rating_keyboard(self) -> InlineKeyboardMarkup:
        keyboard = [
            [InlineKeyboardButton("⭐" * i, callback_data=f'rating_{i}') for i in range(1, 4)],
            [InlineKeyboardButton("⭐" * i, callback_data=f'rating_{i}') for i in range(4, 6)],
            [InlineKeyboardButton(PERSIAN_TEXT['skip_review'], callback_data='skip_review')]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(PERSIAN_TEXT['welcome'], reply_markup=self.get_main_keyboard())

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        help_text = "🤖 راهنمای استفاده از ربات رزرو غذا...\n(متن راهنما اینجا قرار می گیرد)"
        await update.message.reply_text(help_text, reply_markup=self.get_main_keyboard())

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        data = query.data

        # Helper to safely edit messages
        async def safe_edit_message(text: str, markup: InlineKeyboardMarkup):
            try:
                await query.edit_message_text(text=text, reply_markup=markup)
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    logger.warning("Ignored 'Message is not modified' error.")
                else:
                    raise

        if data == 'login':
            await safe_edit_message(PERSIAN_TEXT['login_prompt'], self.get_back_keyboard())
            return LOGIN_USERNAME

        if data == 'back':
            await safe_edit_message(PERSIAN_TEXT['welcome'], self.get_main_keyboard())
            return ConversationHandler.END

        if user_id not in self.user_sessions or not self.user_sessions[user_id].get('logged_in'):
            await safe_edit_message("ابتدا باید وارد حساب کاربری خود شوید.", self.get_main_keyboard())
            return ConversationHandler.END

        if data == 'view_reservations':
            await query.edit_message_text(PERSIAN_TEXT['processing'])
            reservations = await self.api_client.get_reservations()
            if not reservations:
                await safe_edit_message(PERSIAN_TEXT['no_reservations'], self.get_main_keyboard())
                return ConversationHandler.END

            keyboard = []
            for i, res in enumerate(reservations):
                stats = self.review_db.get_food_stats(res['id'])
                rating_info = f" ⭐{stats['average_rating']}" if stats['total_reviews'] > 0 else ""
                button_text = f"{res['name']} - {res['date']}{rating_info}"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f'reserve_{i}')])
            
            keyboard.append([InlineKeyboardButton(PERSIAN_TEXT['back'], callback_data='back')])
            context.user_data['reservations'] = reservations
            await safe_edit_message(PERSIAN_TEXT['select_reservation'], InlineKeyboardMarkup(keyboard))
            return RESERVATION_SELECTION

        elif data.startswith('reserve_'):
            idx = int(data.split('_')[1])
            res = context.user_data['reservations'][idx]
            stats = self.review_db.get_food_stats(res['id'])
            details = (f"{PERSIAN_TEXT['food_details']}\n\n"
                       f"🍽️ نام: {res.get('name', 'نامشخص')}\n"
                       f"📅 تاریخ: {res.get('date', 'نامشخص')}\n"
                       f"⏰ زمان: {res.get('time', 'نامشخص')}\n"
                       f"💰 قیمت: {res.get('price', 0)} ریال\n")
            if stats['total_reviews'] > 0:
                details += f"\n⭐ میانگین امتیاز: {stats['average_rating']}/5 ({stats['total_reviews']} نظر)\n"
            
            keyboard = [
                [InlineKeyboardButton(PERSIAN_TEXT['confirm_reservation'], callback_data=f'confirm_{idx}')],
                [InlineKeyboardButton(PERSIAN_TEXT['view_reviews'], callback_data=f'view_reviews_{idx}')],
                [InlineKeyboardButton(PERSIAN_TEXT['back'], callback_data='view_reservations')]
            ]
            await safe_edit_message(details, InlineKeyboardMarkup(keyboard))
            return RESERVATION_SELECTION

        elif data.startswith('confirm_'):
            idx = int(data.split('_')[1])
            res = context.user_data['reservations'][idx]
            await query.edit_message_text(PERSIAN_TEXT['processing'])
            success = await self.api_client.make_reservation(res['raw'])
            if success:
                context.user_data['last_reservation'] = res
                keyboard = [
                    [InlineKeyboardButton(PERSIAN_TEXT['leave_review'], callback_data='leave_review')],
                    [InlineKeyboardButton(PERSIAN_TEXT['skip_review'], callback_data='skip_review')]
                ]
                await safe_edit_message(PERSIAN_TEXT['reservation_success'], InlineKeyboardMarkup(keyboard))
                return REVIEW_RATING
            else:
                await safe_edit_message(PERSIAN_TEXT['reservation_failed'], self.get_main_keyboard())
                return ConversationHandler.END

        elif data == 'leave_review':
            await safe_edit_message(PERSIAN_TEXT['rating_prompt'], self.get_rating_keyboard())
            return REVIEW_RATING

        elif data == 'skip_review':
            await safe_edit_message(PERSIAN_TEXT['welcome'], self.get_main_keyboard())
            return ConversationHandler.END

        elif data.startswith('rating_'):
            context.user_data['review_rating'] = int(data.split('_')[1])
            await safe_edit_message(PERSIAN_TEXT['comment_prompt'], self.get_back_keyboard())
            return REVIEW_COMMENT

        return ConversationHandler.END

    async def username_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data['username'] = update.message.text.strip()
        await update.message.reply_text(PERSIAN_TEXT['password_prompt'])
        return LOGIN_PASSWORD

    async def password_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        password = update.message.text.strip()
        username = context.user_data.get('username', '')
        user_id = update.effective_user.id
        try:
            await update.message.delete()
        except Exception:
            pass
        
        processing_msg = await update.message.reply_text(PERSIAN_TEXT['processing'])
        success = await self.api_client.login(username, password)
        
        if success:
            self.user_sessions[user_id] = {'logged_in': True, 'username': username}
            await processing_msg.edit_text(PERSIAN_TEXT['login_success'], reply_markup=self.get_main_keyboard())
        else:
            await processing_msg.edit_text(PERSIAN_TEXT['login_failed'], reply_markup=self.get_main_keyboard())
        return ConversationHandler.END

    async def review_comment_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        comment = update.message.text.strip()
        if comment.lower() == '/skip':
            await update.message.reply_text(PERSIAN_TEXT['welcome'], reply_markup=self.get_main_keyboard())
            return ConversationHandler.END

        res = context.user_data.get('last_reservation', {})
        rating = context.user_data.get('review_rating', 5)
        
        self.review_db.add_review(
            user_id=update.effective_user.id,
            user_first_name=update.effective_user.first_name or "کاربر",
            food_id=res.get('id', ''),
            food_name=res.get('name', 'نامشخص'),
            rating=rating,
            comment=comment
        )
        await update.message.reply_text(PERSIAN_TEXT['review_saved'], reply_markup=self.get_main_keyboard())
        return ConversationHandler.END

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text(PERSIAN_TEXT['welcome'], reply_markup=self.get_main_keyboard())
        return ConversationHandler.END

    def create_application(self) -> Application:
        application = (
            Application.builder()
            .token(self.token)
            .post_shutdown(self.api_client.close_session)
            .build()
        )
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', self.start), CallbackQueryHandler(self.button_handler)],
            states={
                LOGIN_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.username_handler)],
                LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.password_handler)],
                RESERVATION_SELECTION: [CallbackQueryHandler(self.button_handler)],
                REVIEW_RATING: [CallbackQueryHandler(self.button_handler)],
                REVIEW_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.review_comment_handler)],
            },
            fallbacks=[CommandHandler('cancel', self.cancel), CallbackQueryHandler(self.button_handler, pattern='^back$')],
            allow_reentry=True
        )
        application.add_handler(conv_handler)
        application.add_handler(CommandHandler('help', self.help_command))
        return application

async def main() -> None:
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        logger.critical("FATAL: TELEGRAM_BOT_TOKEN environment variable is not set.")
        sys.exit(1)

    bot = EnhancedFoodReservationBot(bot_token)
    application = bot.create_application()

    logger.info("Starting bot...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutdown requested.")
