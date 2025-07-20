#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Enhanced Telegram Food Reservation Bot with Review System and Multi-Model AI Fallback
A bot that helps users login to their food reservation account,
view available reservations, make reservations with AI assistance,
and leave reviews for food items.

AI Fallback Sequence: Gemini 2.5 Pro → Gemini 2.5 Flash → Gemini 2.0 Flash → Gemini 1.5 Flash
"""

import logging
import asyncio
import json
import os
import sys
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import aiohttp
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove
)
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
LOGIN_USERNAME, LOGIN_PASSWORD, RESERVATION_SELECTION, AI_HELP, REVIEW_RATING, REVIEW_COMMENT = range(6)

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
    'ai_help_prompt': '🤖 هوش مصنوعی در حال بررسی گزینه‌های غذایی شما است...',
    'ai_recommendation': '🎯 توصیه هوش مصنوعی:',
    'cancel': '❌ لغو',
    'back': '🔙 بازگشت',
    'login': '🔐 ورود',
    'view_reservations': '📋 مشاهده رزروها',
    'ai_help': '🤖 کمک هوش مصنوعی',
    'my_reviews': '📝 نظرات من',
    'logout': '🚪 خروج',
    'help': '❓ راهنما',
    'error': '❌ خطایی رخ داد. لطفاً دوباره تلاش کنید.',
    'session_expired': '⏰ جلسه شما منقضی شده است. لطفاً دوباره وارد شوید.',
    'processing': '⏳ در حال پردازش...',
    'choose_date': '📅 تاریخ مورد نظر را انتخاب کنید:',
    'food_details': '🍽️ جزئیات غذا:',
    'confirm_reservation': '✅ تأیید رزرو',
    'cancel_reservation': '❌ لغو رزرو',
    'leave_review': '📝 ثبت نظر',
    'skip_review': '⏭️ رد کردن',
    'rating_prompt': '⭐ لطفاً امتیاز خود را از ۱ تا ۵ انتخاب کنید:',
    'comment_prompt': '💭 لطفاً نظر خود را درباره این غذا بنویسید:\n(برای رد کردن /skip تایپ کنید)',
    'review_saved': '✅ نظر شما با موفقیت ثبت شد! از شما متشکریم.',
    'view_reviews': '👀 مشاهده نظرات',
    'no_reviews': '📝 هنوز نظری ثبت نشده است.',
    'reviews_title': '📋 نظرات کاربران:',
    'your_reviews_title': '📝 نظرات شما:',
    'average_rating': '⭐ میانگین امتیاز:',
    'total_reviews': '📊 تعداد نظرات:',
    'review_by': '👤 نظر از:',
    'rating': '⭐ امتیاز:',
    'comment': '💭 نظر:',
    'date': '📅 تاریخ:'
}

class ReviewDatabase:
    """Database manager for storing and retrieving reviews"""

    def __init__(self, db_path: str = "reviews.db"):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        """Initialize the database with required tables"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Create reviews table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_first_name TEXT NOT NULL,
                food_id TEXT NOT NULL,
                food_name TEXT NOT NULL,
                rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create index for faster queries
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_food_id ON reviews(food_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_id ON reviews(user_id)')

        conn.commit()
        conn.close()

    def add_review(self, user_id: int, user_first_name: str, food_id: str,
                   food_name: str, rating: int, comment: str = None) -> bool:
        """Add a new review to the database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute('''
                INSERT INTO reviews (user_id, user_first_name, food_id, food_name, rating, comment)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, user_first_name, food_id, food_name, rating, comment))

            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error adding review: {e}")
            return False

    def get_food_reviews(self, food_id: str) -> List[Dict]:
        """Get all reviews for a specific food item"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT user_first_name, rating, comment, created_at
                FROM reviews
                WHERE food_id = ?
                ORDER BY created_at DESC
            ''', (food_id,))

            reviews = []
            for row in cursor.fetchall():
                reviews.append({
                    'user_first_name': row[0],
                    'rating': row[1],
                    'comment': row[2],
                    'created_at': row[3]
                })

            conn.close()
            return reviews
        except Exception as e:
            logger.error(f"Error getting food reviews: {e}")
            return []

    def get_user_reviews(self, user_id: int) -> List[Dict]:
        """Get all reviews by a specific user"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT food_name, rating, comment, created_at
                FROM reviews
                WHERE user_id = ?
                ORDER BY created_at DESC
            ''', (user_id,))

            reviews = []
            for row in cursor.fetchall():
                reviews.append({
                    'food_name': row[0],
                    'rating': row[1],
                    'comment': row[2],
                    'created_at': row[3]
                })

            conn.close()
            return reviews
        except Exception as e:
            logger.error(f"Error getting user reviews: {e}")
            return []

    def get_food_stats(self, food_id: str) -> Dict:
        """Get statistics for a food item"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT AVG(rating), COUNT(*), MIN(rating), MAX(rating)
                FROM reviews
                WHERE food_id = ?
            ''', (food_id,))

            result = cursor.fetchone()
            conn.close()

            if result and result[0] is not None:
                return {
                    'average_rating': round(result[0], 1),
                    'total_reviews': result[1],
                    'min_rating': result[2],
                    'max_rating': result[3]
                }
            else:
                return {
                    'average_rating': 0,
                    'total_reviews': 0,
                    'min_rating': 0,
                    'max_rating': 0
                }
        except Exception as e:
            logger.error(f"Error getting food stats: {e}")
            return {'average_rating': 0, 'total_reviews': 0, 'min_rating': 0, 'max_rating': 0}

    def get_all_reviews_summary(self) -> List[Dict]:
        """Get summary of all reviews for AI analysis"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT food_id, food_name, AVG(rating) as avg_rating, COUNT(*) as review_count,
                       GROUP_CONCAT(comment, ' | ') as all_comments
                FROM reviews
                WHERE comment IS NOT NULL AND comment != ''
                GROUP BY food_id, food_name
                ORDER BY avg_rating DESC, review_count DESC
            ''')

            summaries = []
            for row in cursor.fetchall():
                summaries.append({
                    'food_id': row[0],
                    'food_name': row[1],
                    'average_rating': round(row[2], 1),
                    'review_count': row[3],
                    'comments': row[4] if row[4] else ''
                })

            conn.close()
            return summaries
        except Exception as e:
            logger.error(f"Error getting reviews summary: {e}")
            return []

class FoodReservationAPI:
    """API client for food reservation system"""

    def __init__(self):
        self.base_url = "https://food.gums.ac.ir"
        self.session = None
        self.cookies = {}
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'fa-IR,fa;q=0.9,en;q=0.8',
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest'
        }

    async def create_session(self):
        """Create aiohttp session"""
        if not self.session:
            self.session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=30)
            )

    async def close_session(self):
        """Close aiohttp session"""
        if self.session:
            await self.session.close()
            self.session = None

    async def login(self, username: str, password: str) -> bool:
        """Login to the food reservation system"""
        try:
            await self.create_session()

            # First, get the login page to obtain any necessary tokens
            async with self.session.get(f"{self.base_url}/login") as response:
                if response.status == 200:
                    # Update cookies from the response
                    self.cookies.update({cookie.key: cookie.value for cookie in response.cookies})

            # Prepare login data
            login_data = {
                "username": username,
                "password": password
            }

            # Perform login
            async with self.session.post(
                f"{self.base_url}/api/auth/login",
                json=login_data,
                cookies=self.cookies
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get('success', False):
                        # Update cookies after successful login
                        self.cookies.update({cookie.key: cookie.value for cookie in response.cookies})
                        return True
                return False

        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    async def get_reservations(self) -> List[Dict]:
        """Get available reservations"""
        try:
            if not self.session:
                return []

            async with self.session.get(
                f"{self.base_url}/api/reservations",
                cookies=self.cookies
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get('data', [])
                return []

        except Exception as e:
            logger.error(f"Get reservations error: {e}")
            return []

    async def make_reservation(self, reservation_id: str) -> bool:
        """Make a reservation"""
        try:
            if not self.session:
                return False

            reservation_data = {
                "reservation_id": reservation_id
            }

            async with self.session.post(
                f"{self.base_url}/api/reservations/create",
                json=reservation_data,
                cookies=self.cookies
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get('success', False)
                return False

        except Exception as e:
            logger.error(f"Make reservation error: {e}")
            return False

    async def cancel_reservation(self, reservation_id: str) -> bool:
        """Cancel a reservation"""
        try:
            if not self.session:
                return False

            async with self.session.delete(
                f"{self.base_url}/api/reservations/{reservation_id}",
                cookies=self.cookies
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get('success', False)
                return False

        except Exception as e:
            logger.error(f"Cancel reservation error: {e}")
            return False

class MultiModelGeminiAI:
    """Enhanced Gemini AI integration with multi-model fallback sequence"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        # AI Model URLs in fallback order: 2.5 Pro → 2.5 Flash → 2.0 Flash → 1.5 Flash
        self.model_urls = [
            {
                'name': 'Gemini 2.5 Pro',
                'url': 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent'
            },
            {
                'name': 'Gemini 2.5 Flash',
                'url': 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent'
            },
            {
                'name': 'Gemini 2.0 Flash',
                'url': 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent'
            },
            {
                'name': 'Gemini 1.5 Flash',
                'url': 'https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent'
            }
        ]

    async def get_food_recommendation(self, food_options: List[Dict], review_db: ReviewDatabase) -> str:
        """Get AI recommendation for food selection using reviews data with multi-model fallback"""
        try:
            # Get review summaries for context
            review_summaries = review_db.get_all_reviews_summary()

            # Create enhanced prompt with reviews data
            food_descriptions = []
            for option in food_options:
                food_id = option.get('id', '')
                food_name = option.get('name', 'نامشخص')

                desc = f"- {food_name}"
                if option.get('description'):
                    desc += f": {option['description']}"
                if option.get('calories'):
                    desc += f" (کالری: {option['calories']})"
                if option.get('price'):
                    desc += f" (قیمت: {option['price']} تومان)"

                # Add review data if available
                stats = review_db.get_food_stats(food_id)
                if stats['total_reviews'] > 0:
                    desc += f" (امتیاز: {stats['average_rating']}/5 از {stats['total_reviews']} نظر)"

                    # Add recent reviews
                    reviews = review_db.get_food_reviews(food_id)
                    if reviews:
                        recent_comments = []
                        for review in reviews[:3]:  # Last 3 reviews
                            if review['comment']:
                                recent_comments.append(f"'{review['comment']}' - {review['user_first_name']}")
                        if recent_comments:
                            desc += f"\n  نظرات اخیر: {'; '.join(recent_comments)}"

                food_descriptions.append(desc)

            # Create comprehensive prompt
            prompt = f"""
شما یک متخصص تغذیه و مشاور غذایی هستید که باید بهترین گزینه غذایی را از میان گزینه‌های زیر انتخاب کنید:

{chr(10).join(food_descriptions)}

اطلاعات اضافی از نظرات کاربران قبلی:
"""

            # Add review context
            if review_summaries:
                prompt += "\nخلاصه نظرات کاربران:\n"
                for summary in review_summaries[:10]:  # Top 10 reviewed items
                    prompt += f"- {summary['food_name']}: امتیاز {summary['average_rating']}/5 ({summary['review_count']} نظر)\n"
                    if summary['comments']:
                        # Get first few words of comments
                        comments_preview = summary['comments'][:200] + "..." if len(summary['comments']) > 200 else summary['comments']
                        prompt += f"  نمونه نظرات: {comments_preview}\n"

            prompt += """

لطفاً با در نظر گیری موارد زیر، بهترین گزینه را توصیه کنید:
1. ارزش غذایی و سلامتی
2. تعادل مواد مغذی
3. کیفیت مواد اولیه (بر اساس نظرات کاربران)
4. رضایت کاربران قبلی
5. مناسب بودن برای وعده غذایی
6. نسبت قیمت به کیفیت

پاسخ خود را به صورت مختصر و مفید ارائه دهید، دلیل انتخاب خود را بیان کنید و در صورت وجود، از نظرات کاربران قبلی نیز استفاده کنید.
"""

            # Try models in fallback sequence
            for model in self.model_urls:
                logger.info(f"Trying {model['name']}...")
                recommendation = await self._call_gemini_api(model['url'], prompt, model['name'])

                if recommendation and not self._is_error_response(recommendation):
                    logger.info(f"Successfully got recommendation from {model['name']}")
                    return f"🤖 توصیه از {model['name']}:\n\n{recommendation}"
                else:
                    logger.warning(f"{model['name']} failed, trying next model...")

            # If all models fail
            logger.error("All AI models failed to provide recommendation")
            return "متأسفانه تمام مدل‌های هوش مصنوعی در دسترس نیستند. لطفاً بر اساس سلیقه خود انتخاب کنید."

        except Exception as e:
            logger.error(f"AI recommendation error: {e}")
            return "خطا در دریافت توصیه هوش مصنوعی. لطفلاً بر اساس سلیقه خود انتخاب کنید."

    def _is_error_response(self, response: str) -> bool:
        """Check if the response indicates an error"""
        error_indicators = [
            "خطا", "error", "failed", "متأسفانه", "نمی‌توانم",
            "در دسترس نیست", "مشکل", "امکان‌پذیر نیست"
        ]
        return any(indicator in response.lower() for indicator in error_indicators)

    async def _call_gemini_api(self, url: str, prompt: str, model_name: str) -> str:
        """Call Gemini API with the given URL and prompt"""
        try:
            # Modified: Use X-goog-api-key header
            headers = {
                'Content-Type': 'application/json',
                'X-goog-api-key': self.api_key, # Use API key in header
            }

            data = {
                "contents": [{
                    "parts": [{
                        "text": prompt
                    }]
                }],
                "generationConfig": {
                    "temperature": 0.7,
                    "topK": 40,
                    "topP": 0.95,
                    "maxOutputTokens": 1024,
                }
            }

            async with aiohttp.ClientSession() as session:
                # Modified: Remove API key from URL query parameter
                async with session.post(
                    url, # URL without ?key=
                    headers=headers,
                    json=data,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        if 'candidates' in result and len(result['candidates']) > 0:
                            content = result['candidates'][0]['content']['parts'][0]['text']
                            return content
                    else:
                        logger.error(f"{model_name} API error: {response.status}")
                        if response.status == 429:
                            logger.warning(f"{model_name} rate limited")
                        elif response.status == 403:
                            logger.warning(f"{model_name} access forbidden")

                    return None

        except asyncio.TimeoutError:
            logger.error(f"{model_name} API timeout")
            return None
        except Exception as e:
            logger.error(f"{model_name} API call error: {e}")
            return None

class EnhancedFoodReservationBot:
    """Enhanced bot class with review system and multi-model AI"""

    def __init__(self, token: str, gemini_api_key: str):
        self.token = token
        self.api_client = FoodReservationAPI()
        self.ai_client = MultiModelGeminiAI(gemini_api_key)
        self.review_db = ReviewDatabase()
        self.user_sessions = {}  # Store user session data

    def get_main_keyboard(self) -> InlineKeyboardMarkup:
        """Get main menu keyboard"""
        keyboard = [
            [InlineKeyboardButton(PERSIAN_TEXT['login'], callback_data='login')],
            [InlineKeyboardButton(PERSIAN_TEXT['view_reservations'], callback_data='view_reservations')],
            [InlineKeyboardButton(PERSIAN_TEXT['ai_help'], callback_data='ai_help')],
            [InlineKeyboardButton(PERSIAN_TEXT['my_reviews'], callback_data='my_reviews')],
            [InlineKeyboardButton(PERSIAN_TEXT['help'], callback_data='help')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_back_keyboard(self) -> InlineKeyboardMarkup:
        """Get back button keyboard"""
        keyboard = [
            [InlineKeyboardButton(PERSIAN_TEXT['back'], callback_data='back')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_rating_keyboard(self) -> InlineKeyboardMarkup:
        """Get rating selection keyboard"""
        keyboard = [
            [
                InlineKeyboardButton("⭐", callback_data='rating_1'),
                InlineKeyboardButton("⭐⭐", callback_data='rating_2'),
                InlineKeyboardButton("⭐⭐⭐", callback_data='rating_3')
            ],
            [
                InlineKeyboardButton("⭐⭐⭐⭐", callback_data='rating_4'),
                InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data='rating_5')
            ],
            [InlineKeyboardButton(PERSIAN_TEXT['skip_review'], callback_data='skip_review')]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start command handler"""
        await update.message.reply_text(
            PERSIAN_TEXT['welcome'],
            reply_markup=self.get_main_keyboard()
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Help command handler"""
        help_text = """
🤖 راهنمای استفاده از ربات رزرو غذا

📋 امکانات:
• ورود به حساب کاربری
• مشاهده رزروهای موجود
• انجام رزرو غذا
• کمک هوش مصنوعی برای انتخاب غذا (با 4 مدل پشتیبان)
• ثبت نظر و امتیاز برای غذاها
• مشاهده نظرات خود
• لغو رزرو

🔐 برای شروع، ابتدا وارد حساب کاربری خود شوید.
📱 از دکمه‌های زیر پیام‌ها استفاده کنید.
⭐ پس از رزرو، نظر خود را ثبت کنید تا به بهبود توصیه‌ها کمک کنید.
🤖 سیستم هوش مصنوعی از 4 مدل مختلف برای بهترین توصیه استفاده می‌کند.
❓ برای کمک بیشتر از /help استفاده کنید.
        """
        await update.message.reply_text(help_text, reply_markup=self.get_main_keyboard())

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle inline keyboard button presses"""
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id
        data = query.data

        if data == 'login':
            await query.edit_message_text(
                PERSIAN_TEXT['login_prompt'],
                reply_markup=self.get_back_keyboard()
            )
            return LOGIN_USERNAME

        elif data == 'view_reservations':
            if user_id not in self.user_sessions or not self.user_sessions[user_id].get('logged_in'):
                await query.edit_message_text(
                    "ابتدا باید وارد حساب کاربری خود شوید.",
                    reply_markup=self.get_main_keyboard()
                )
                return ConversationHandler.END

            await query.edit_message_text(PERSIAN_TEXT['processing'])
            reservations = await self.api_client.get_reservations()

            if not reservations:
                await query.edit_message_text(
                    PERSIAN_TEXT['no_reservations'],
                    reply_markup=self.get_main_keyboard()
                )
                return ConversationHandler.END

            # Create reservation selection keyboard with reviews info
            keyboard = []
            for i, reservation in enumerate(reservations):
                name = reservation.get('name', f'رزرو {i+1}')
                date = reservation.get('date', '')
                food_id = reservation.get('id', '')

                # Add rating info if available
                stats = self.review_db.get_food_stats(food_id)
                rating_info = ""
                if stats['total_reviews'] > 0:
                    rating_info = f" ⭐{stats['average_rating']}"

                button_text = f"{name} - {date}{rating_info}"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f'reserve_{i}')])

            keyboard.append([InlineKeyboardButton(PERSIAN_TEXT['ai_help'], callback_data='ai_help_reservations')])
            keyboard.append([InlineKeyboardButton(PERSIAN_TEXT['back'], callback_data='back')])

            context.user_data['reservations'] = reservations

            await query.edit_message_text(
                PERSIAN_TEXT['select_reservation'],
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return RESERVATION_SELECTION

        elif data == 'ai_help' or data == 'ai_help_reservations':
            if user_id not in self.user_sessions or not self.user_sessions[user_id].get('logged_in'):
                await query.edit_message_text(
                    "ابتدا باید وارد حساب کاربری خود شوید.",
                    reply_markup=self.get_main_keyboard()
                )
                return ConversationHandler.END

            await query.edit_message_text(PERSIAN_TEXT['ai_help_prompt'])
            reservations = await self.api_client.get_reservations()

            if reservations:
                recommendation = await self.ai_client.get_food_recommendation(reservations, self.review_db)
                await query.edit_message_text(
                    f"{PERSIAN_TEXT['ai_recommendation']}\n\n{recommendation}",
                    reply_markup=self.get_main_keyboard()
                )
            else:
                await query.edit_message_text(
                    PERSIAN_TEXT['no_reservations'],
                    reply_markup=self.get_main_keyboard()
                )
            return ConversationHandler.END

        elif data == 'my_reviews':
            if user_id not in self.user_sessions or not self.user_sessions[user_id].get('logged_in'):
                await query.edit_message_text(
                    "ابتدا باید وارد حساب کاربری خود شوید.",
                    reply_markup=self.get_main_keyboard()
                )
                return ConversationHandler.END

            reviews = self.review_db.get_user_reviews(user_id)

            if not reviews:
                await query.edit_message_text(
                    "شما هنوز نظری ثبت نکرده‌اید.",
                    reply_markup=self.get_main_keyboard()
                )
                return ConversationHandler.END

            # Format reviews for display
            reviews_text = f"{PERSIAN_TEXT['your_reviews_title']}\n\n"
            for review in reviews[:10]:  # Show last 10 reviews
                reviews_text += f"🍽️ {review['food_name']}\n"
                reviews_text += f"⭐ امتیاز: {review['rating']}/5\n"
                if review['comment']:
                    reviews_text += f"💭 نظر: {review['comment']}\n"
                reviews_text += f"📅 {review['created_at'][:10]}\n\n"

            await query.edit_message_text(
                reviews_text,
                reply_markup=self.get_main_keyboard()
            )
            return ConversationHandler.END

        elif data == 'back':
            await query.edit_message_text(
                PERSIAN_TEXT['welcome'],
                reply_markup=self.get_main_keyboard()
            )
            return ConversationHandler.END

        elif data.startswith('reserve_'):
            reservation_index = int(data.split('_')[1])
            reservations = context.user_data.get('reservations', [])

            if reservation_index < len(reservations):
                reservation = reservations[reservation_index]
                food_id = reservation.get('id', '')

                # Show reservation details with reviews
                details = f"{PERSIAN_TEXT['food_details']}\n\n"
                details += f"🍽️ نام: {reservation.get('name', 'نامشخص')}\n"
                details += f"📅 تاریخ: {reservation.get('date', 'نامشخص')}\n"
                details += f"⏰ زمان: {reservation.get('time', 'نامشخص')}\n"
                if reservation.get('description'):
                    details += f"📝 توضیحات: {reservation['description']}\n"
                if reservation.get('price'):
                    details += f"💰 قیمت: {reservation['price']} تومان\n"

                # Add review statistics
                stats = self.review_db.get_food_stats(food_id)
                if stats['total_reviews'] > 0:
                    details += f"\n⭐ میانگین امتیاز: {stats['average_rating']}/5\n"
                    details += f"📊 تعداد نظرات: {stats['total_reviews']}\n"

                keyboard = [
                    [InlineKeyboardButton(PERSIAN_TEXT['confirm_reservation'], callback_data=f'confirm_{reservation_index}')],
                    [InlineKeyboardButton(PERSIAN_TEXT['view_reviews'], callback_data=f'view_reviews_{reservation_index}')],
                    [InlineKeyboardButton(PERSIAN_TEXT['back'], callback_data='view_reservations')]
                ]

                await query.edit_message_text(
                    details,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            return RESERVATION_SELECTION

        elif data.startswith('view_reviews_'):
            reservation_index = int(data.split('_')[2])
            reservations = context.user_data.get('reservations', [])

            if reservation_index < len(reservations):
                reservation = reservations[reservation_index]
                food_id = reservation.get('id', '')
                food_name = reservation.get('name', 'نامشخص')

                reviews = self.review_db.get_food_reviews(food_id)

                if not reviews:
                    reviews_text = f"📝 نظرات برای {food_name}\n\n{PERSIAN_TEXT['no_reviews']}"
                else:
                    reviews_text = f"📝 نظرات برای {food_name}\n\n"
                    for review in reviews[:5]:  # Show last 5 reviews
                        reviews_text += f"👤 {review['user_first_name']}\n"
                        reviews_text += f"⭐ امتیاز: {review['rating']}/5\n"
                        if review['comment']:
                            reviews_text += f"💭 {review['comment']}\n"
                        reviews_text += f"📅 {review['created_at'][:10]}\n\n"

                keyboard = [
                    [InlineKeyboardButton(PERSIAN_TEXT['back'], callback_data=f'reserve_{reservation_index}')]
                ]

                await query.edit_message_text(
                    reviews_text,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            return RESERVATION_SELECTION

        elif data.startswith('confirm_'):
            reservation_index = int(data.split('_')[1])
            reservations = context.user_data.get('reservations', [])

            if reservation_index < len(reservations):
                reservation = reservations[reservation_index]
                reservation_id = reservation.get('id', '')

                await query.edit_message_text(PERSIAN_TEXT['processing'])

                success = await self.api_client.make_reservation(reservation_id)

                if success:
                    # Store reservation info for review
                    context.user_data['last_reservation'] = reservation

                    keyboard = [
                        [InlineKeyboardButton(PERSIAN_TEXT['leave_review'], callback_data='leave_review')],
                        [InlineKeyboardButton(PERSIAN_TEXT['skip_review'], callback_data='skip_review')]
                    ]

                    await query.edit_message_text(
                        PERSIAN_TEXT['reservation_success'],
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    return REVIEW_RATING
                else:
                    await query.edit_message_text(
                        PERSIAN_TEXT['reservation_failed'],
                        reply_markup=self.get_main_keyboard()
                    )

            return ConversationHandler.END

        elif data == 'leave_review':
            await query.edit_message_text(
                PERSIAN_TEXT['rating_prompt'],
                reply_markup=self.get_rating_keyboard()
            )
            return REVIEW_RATING

        elif data == 'skip_review':
            await query.edit_message_text(
                PERSIAN_TEXT['welcome'],
                reply_markup=self.get_main_keyboard()
            )
            return ConversationHandler.END

        elif data.startswith('rating_'):
            rating = int(data.split('_')[1])
            context.user_data['review_rating'] = rating

            await query.edit_message_text(
                PERSIAN_TEXT['comment_prompt'],
                reply_markup=self.get_back_keyboard()
            )
            return REVIEW_COMMENT

        elif data == 'help':
            await self.help_command(update, context)
            return ConversationHandler.END

        return ConversationHandler.END

    async def username_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle username input"""
        username = update.message.text.strip()
        context.user_data['username'] = username

        await update.message.reply_text(
            PERSIAN_TEXT['password_prompt'],
            reply_markup=self.get_back_keyboard()
        )
        return LOGIN_PASSWORD

    async def password_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle password input"""
        password = update.message.text.strip()
        username = context.user_data.get('username', '')
        user_id = update.effective_user.id

        # Delete the password message for security
        try:
            await update.message.delete()
        except:
            pass

        # Show processing message
        processing_msg = await update.message.reply_text(PERSIAN_TEXT['processing'])

        # Attempt login
        success = await self.api_client.login(username, password)

        if success:
            # Store user session
            self.user_sessions[user_id] = {
                'logged_in': True,
                'username': username,
                'login_time': datetime.now()
            }

            await processing_msg.edit_text(
                PERSIAN_TEXT['login_success'],
                reply_markup=self.get_main_keyboard()
            )
        else:
            await processing_msg.edit_text(
                PERSIAN_TEXT['login_failed'],
                reply_markup=self.get_main_keyboard()
            )

        return ConversationHandler.END

    async def review_comment_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle review comment input"""
        comment = update.message.text.strip()
        user_id = update.effective_user.id
        user_first_name = update.effective_user.first_name or "کاربر"

        # Skip if user typed /skip
        if comment.lower() == '/skip':
            await update.message.reply_text(
                PERSIAN_TEXT['welcome'],
                reply_markup=self.get_main_keyboard()
            )
            return ConversationHandler.END

        # Get reservation and rating info
        reservation = context.user_data.get('last_reservation', {})
        rating = context.user_data.get('review_rating', 5)

        food_id = reservation.get('id', '')
        food_name = reservation.get('name', 'نامشخص')

        # Save review to database
        success = self.review_db.add_review(
            user_id=user_id,
            user_first_name=user_first_name,
            food_id=food_id,
            food_name=food_name,
            rating=rating,
            comment=comment
        )

        if success:
            await update.message.reply_text(
                PERSIAN_TEXT['review_saved'],
                reply_markup=self.get_main_keyboard()
            )
        else:
            await update.message.reply_text(
                PERSIAN_TEXT['error'],
                reply_markup=self.get_main_keyboard()
            )

        return ConversationHandler.END

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel conversation"""
        await update.message.reply_text(
            PERSIAN_TEXT['welcome'],
            reply_markup=self.get_main_keyboard()
        )
        return ConversationHandler.END

    def create_application(self) -> Application:
        """Create and configure the bot application"""
        application = Application.builder().token(self.token).build()

        # Conversation handler for login and reservations
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', self.start),
                CallbackQueryHandler(self.button_handler)
            ],
            states={
                LOGIN_USERNAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.username_handler),
                    CallbackQueryHandler(self.button_handler)
                ],
                LOGIN_PASSWORD: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.password_handler),
                    CallbackQueryHandler(self.button_handler)
                ],
                RESERVATION_SELECTION: [
                    CallbackQueryHandler(self.button_handler)
                ],
                AI_HELP: [
                    CallbackQueryHandler(self.button_handler)
                ],
                REVIEW_RATING: [
                    CallbackQueryHandler(self.button_handler)
                ],
                REVIEW_COMMENT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.review_comment_handler),
                    CallbackQueryHandler(self.button_handler)
                ]
            },
            fallbacks=[
                CommandHandler('cancel', self.cancel),
                CallbackQueryHandler(self.button_handler)
            ]
        )

        application.add_handler(conv_handler)
        application.add_handler(CommandHandler('help', self.help_command))

        return application

async def main():
    """Main function to run the bot"""
    # Get configuration from environment variables
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    gemini_api_key = os.getenv('GEMINI_API_KEY')

    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is required")
        sys.exit(1)

    if not gemini_api_key:
        logger.error("GEMINI_API_KEY environment variable is required")
        sys.exit(1)

    # Create and run the bot
    bot = EnhancedFoodReservationBot(bot_token, gemini_api_key)
    application = bot.create_application()

    logger.info("Starting Enhanced Food Reservation Bot with Multi-Model AI Fallback...")
    logger.info("AI Fallback Sequence: Gemini 2.5 Pro → Gemini 2.5 Flash → Gemini 2.0 Flash → Gemini 1.5 Flash")

    try:
        # Explicitly initialize the application
        await application.initialize()
        # Run the bot
        await application.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot error: {e}")
    finally:
        # Ensure the application is stopped gracefully before closing the session
        # This check prevents trying to shutdown an application that never fully started or is already stopped
        if application.running: # Check if the application is currently running
            logger.info("Shutting down Telegram bot application...")
            await application.shutdown()
        # Clean up API client session
        await bot.api_client.close_session()

if __name__ == '__main__':
    asyncio.run(main())
