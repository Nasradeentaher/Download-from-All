#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import sqlite3
import logging
import asyncio
from datetime import datetime
from threading import Thread
from urllib.parse import urlparse
import re

from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram.constants import ParseMode
import yt_dlp
from dotenv import load_dotenv

# تحميل متغيرات البيئة
load_dotenv()

# إعداد Flask للـ Webhook
app = Flask(__name__)

# إعداد logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class ConfigManager:
    """إدارة إعدادات البوت"""
    def __init__(self, config_file='config.json'):
        self.config_file = config_file
        self.config = self._load_config()
    
    def _load_config(self):
        """تحميل الإعدادات من الملف"""
        default_config = {
            "welcome_message": "🎉 مرحباً بك في بوت التحميل المتقدم!\n\n📥 يمكنك تحميل المحتوى من أكثر من 1000 منصة\n\n🔗 أرسل الرابط وسأقوم بتحميله لك",
            "subscription_message": "📢 للاستفادة من البوت، يجب عليك الاشتراك في القناة أولاً:",
            "topic_mode": False,
            "bot_mode": "bot",
            "download_quality": "video_hd"
        }
        
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    default_config.update(loaded_config)
            return default_config
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            return default_config
    
    def _save_config(self):
        """حفظ الإعدادات في الملف"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Error saving config: {e}")
    
    def get(self, key, default=None):
        """الحصول على قيمة إعداد"""
        return self.config.get(key, default)
    
    def set(self, key, value):
        """تعيين قيمة إعداد"""
        self.config[key] = value
        self._save_config()

class SubscriptionManager:
    """نظام إدارة الاشتراكات"""
    def __init__(self, channel_username, db_path='database/subscriptions.db'):
        self.channel_username = channel_username
        self.db_path = db_path
        self.admin_ids = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]
        self.init_database()
    
    def init_database(self):
        """تهيئة قاعدة البيانات"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # جدول المستخدمين
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT,
                is_subscribed BOOLEAN DEFAULT FALSE,
                subscription_checked_at DATETIME,
                first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_activity DATETIME DEFAULT CURRENT_TIMESTAMP,
                total_downloads INTEGER DEFAULT 0,
                is_banned BOOLEAN DEFAULT FALSE,
                is_admin BOOLEAN DEFAULT FALSE,
                chat_mode TEXT DEFAULT "normal"
            )
        ''')
        
        # جدول التحميلات
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                url TEXT,
                platform TEXT,
                quality TEXT,
                file_size INTEGER,
                download_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                success BOOLEAN DEFAULT TRUE,
                error_message TEXT,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        conn.commit()
        conn.close()
    
    async def check_subscription(self, context, user_id):
        """التحقق من اشتراك المستخدم"""
        try:
            # التحقق من أن المستخدم أدمن
            if user_id in self.admin_ids:
                return True
            
            # التحقق من الاشتراك في القناة
            if self.channel_username:
                member = await context.bot.get_chat_member(
                    chat_id=f"@{self.channel_username}",
                    user_id=user_id
                )
                is_subscribed = member.status in ['member', 'administrator', 'creator']
                
                # تحديث حالة الاشتراك في قاعدة البيانات
                self.update_user_info(user_id, 
                                    is_subscribed=is_subscribed,
                                    subscription_checked_at=datetime.now())
                
                return is_subscribed
            
            return True
            
        except Exception as e:
            logger.error(f"Error checking subscription: {e}")
            return False
    
    def update_user_info(self, user_id, **kwargs):
        """تحديث معلومات المستخدم"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # التحقق من وجود المستخدم
            cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
            if not cursor.fetchone():
                cursor.execute("""
                    INSERT INTO users (user_id, is_admin) 
                    VALUES (?, ?)
                """, (user_id, user_id in self.admin_ids))
            
            # تحديث البيانات
            if kwargs:
                set_clause = ", ".join([f"{key} = ?" for key in kwargs.keys()])
                values = list(kwargs.values()) + [user_id]
                cursor.execute(f"UPDATE users SET {set_clause} WHERE user_id = ?", values)
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Error updating user info: {e}")
    
    def is_user_admin(self, user_id):
        """التحقق من أن المستخدم أدمن"""
        return user_id in self.admin_ids

class AdvancedDownloader:
    """نظام التحميل المتقدم"""
    def __init__(self, download_path='./downloads'):
        self.download_path = download_path
        os.makedirs(download_path, exist_ok=True)
        
        # المنصات المدعومة
        self.supported_domains = {
            'youtube.com': 'YouTube', 'youtu.be': 'YouTube',
            'instagram.com': 'Instagram', 'tiktok.com': 'TikTok',
            'twitter.com': 'Twitter/X', 'x.com': 'Twitter/X',
            'facebook.com': 'Facebook', 'fb.watch': 'Facebook',
            'threads.net': 'Threads', 't.me': 'Telegram'
        }
        
        # إعدادات الجودة
        self.quality_presets = {
            'video_hd': 'best[height<=720][ext=mp4]/best[height<=720]/best[ext=mp4]/best',
            'video_sd': 'best[height<=480][ext=mp4]/best[height<=480]/best[ext=mp4]/best',
            'video_mobile': 'best[height<=360][ext=mp4]/best[height<=360]/best[ext=mp4]/best',
            'video_best': 'best[ext=mp4]/best',
            'audio_only': 'bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best'
        }
    
    def detect_platform(self, url):
        """اكتشاف المنصة من الرابط"""
        try:
            domain = urlparse(url).netloc.lower()
            for supported_domain, platform in self.supported_domains.items():
                if supported_domain in domain:
                    return platform
            return "Unknown"
        except:
            return "Unknown"
    
    def extract_urls_from_text(self, text):
        """استخراج الروابط من النص"""
        url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        return re.findall(url_pattern, text)
    
    async def download_content(self, url, quality='video_hd'):
        """تحميل المحتوى"""
        try:
            platform = self.detect_platform(url)
            
            ydl_opts = {
                'format': self.quality_presets.get(quality, self.quality_presets['video_hd']),
                'outtmpl': f'{self.download_path}/%(title)s.%(ext)s',
                'noplaylist': True,
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                
                return {
                    'success': True,
                    'title': info.get('title', 'Unknown'),
                    'platform': platform,
                    'file_path': ydl.prepare_filename(info),
                    'duration': info.get('duration'),
                    'file_size': info.get('filesize')
                }
                
        except Exception as e:
            logger.error(f"Download error: {e}")
            return {
                'success': False,
                'error': str(e)
            }

class TelegramDownloaderBot:
    """الفئة الرئيسية للبوت"""
    def __init__(self):
        self.bot_token = os.getenv('BOT_TOKEN')
        self.channel_username = os.getenv('CHANNEL_USERNAME', '').replace('@', '')
        
        # إنشاء المدراء
        self.config_manager = ConfigManager()
        self.subscription_manager = SubscriptionManager(self.channel_username)
        self.downloader = AdvancedDownloader()
        
        # إنشاء التطبيق
        self.application = Application.builder().token(self.bot_token).build()
        self._setup_handlers()
    
    def _setup_handlers(self):
        """إعداد معالجات الأوامر"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("admin", self.admin_command))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
    
    async def start_command(self, update, context):
        """معالج أمر البداية"""
        user = update.effective_user
        user_id = user.id
        
        # تحديث معلومات المستخدم
        self.subscription_manager.update_user_info(
            user_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=user.language_code,
            last_activity=datetime.now()
        )
        
        # التحقق من الاشتراك
        if not await self.subscription_manager.check_subscription(context, user_id):
            keyboard = [[InlineKeyboardButton("🔗 اشترك في القناة", url=f"https://t.me/{self.channel_username}")],
                       [InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="check_subscription")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                self.config_manager.get('subscription_message'),
                reply_markup=reply_markup
            )
            return
        
        # إظهار الواجهة الرئيسية
        await self.show_main_interface(update, context)
    
    async def admin_command(self, update, context):
        """معالج لوحة التحكم الإدارية"""
        user_id = update.effective_user.id
        
        if not self.subscription_manager.is_user_admin(user_id):
            await update.message.reply_text("❌ ليس لديك صلاحية للوصول إلى لوحة التحكم")
            return
        
        await self.show_admin_panel(update, context)
    
    async def show_main_interface(self, update, context):
        """إظهار الواجهة الرئيسية"""
        keyboard = [
            ["📥 تحميل من رابط"],
            ["👨‍💼 التواصل مع الأدمن"]
        ]
        
        from telegram import ReplyKeyboardMarkup
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(
            self.config_manager.get('welcome_message'),
            reply_markup=reply_markup
        )
    
    async def show_admin_panel(self, update, context):
        """إظهار لوحة التحكم الإدارية"""
        keyboard = [
            ["🤖 استلام الرسائل في البوت", "👥 استلام الرسائل في المجموعة"],
            ["📁 وضع المواضيع مفعل/معطل"],
            ["✏️ تعديل رسالة الترحيب", "✏️ تعديل رسالة الاشتراك"],
            ["📥 خيارات التحميل"],
            ["📊 إحصائيات المستخدمين", "📈 إحصائيات التحميل"],
            ["📢 بث رسالة"],
            ["🚫 حظر مستخدم", "✅ إلغاء حظر مستخدم"]
        ]
        
        from telegram import ReplyKeyboardMarkup
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(
            "🔧 **لوحة التحكم الإدارية**\n\nاختر الإجراء المطلوب:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def handle_callback(self, update, context):
        """معالج الأزرار التفاعلية"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "check_subscription":
            user_id = query.from_user.id
            if await self.subscription_manager.check_subscription(context, user_id):
                await query.edit_message_text("✅ تم التحقق من الاشتراك بنجاح!")
                await self.show_main_interface(query, context)
            else:
                await query.edit_message_text("❌ لم يتم العثور على اشتراك. يرجى الاشتراك في القناة أولاً.")
    
    async def handle_message(self, update, context):
        """معالج الرسائل النصية"""
        user_id = update.effective_user.id
        text = update.message.text
        
        # التحقق من الاشتراك
        if not await self.subscription_manager.check_subscription(context, user_id):
            await self.start_command(update, context)
            return
        
        # معالجة الروابط
        urls = self.downloader.extract_urls_from_text(text)
        if urls:
            await self.process_download(update, context, urls[0])
            return
        
        # معالجة أوامر الأدمن
        if self.subscription_manager.is_user_admin(user_id):
            await self.handle_admin_commands(update, context, text)
        else:
            await update.message.reply_text("🔗 أرسل رابط المحتوى الذي تريد تحميله")
    
    async def process_download(self, update, context, url):
        """معالجة طلب التحميل"""
        user_id = update.effective_user.id
        
        # إرسال رسالة التحميل
        status_message = await update.message.reply_text("⏳ جاري التحميل...")
        
        try:
            # تحميل المحتوى
            result = await self.downloader.download_content(url)
            
            if result['success']:
                # إرسال الملف
                await status_message.edit_text("📤 جاري رفع الملف...")
                
                with open(result['file_path'], 'rb') as file:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=file,
                        caption=f"✅ تم التحميل بنجاح\n📱 المنصة: {result['platform']}\n📄 العنوان: {result['title']}"
                    )
                
                # تحديث الإحصائيات
                self.subscription_manager.update_user_info(
                    user_id,
                    total_downloads=1,  # يجب تحديث هذا ليكون تراكمي
                    last_activity=datetime.now()
                )
                
                await status_message.delete()
                
            else:
                await status_message.edit_text(f"❌ فشل التحميل: {result['error']}")
                
        except Exception as e:
            logger.error(f"Download process error: {e}")
            await status_message.edit_text("❌ حدث خطأ أثناء التحميل")
    
    async def handle_admin_commands(self, update, context, text):
        """معالجة أوامر الأدمن"""
        # هنا يمكن إضافة معالجة أوامر الأدمن المختلفة
        await update.message.reply_text("🔧 معالجة أوامر الأدمن قيد التطوير")

# إعداد Flask
@app.route('/')
def index():
    return "Bot is running!"

@app.route('/webhook', methods=['POST'])
def webhook():
    """معالج الـ Webhook"""
    try:
        update = Update.de_json(request.get_json(), bot.application.bot)
        asyncio.create_task(bot.application.process_update(update))
        return "OK"
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "Error", 500

# إنشاء البوت
bot = TelegramDownloaderBot()

# دالة للحفاظ على البوت نشطاً
def keep_alive():
    """دالة للحفاظ على البوت نشطاً على Replit"""
    Thread(target=lambda: app.run(host='0.0.0.0', port=8080, debug=False)).start()

async def setup_webhook():
    """إعداد الـ Webhook"""
    # في Replit، سيكون الرابط تلقائياً
    webhook_url = f"https://{os.getenv('REPL_SLUG', 'your-repl-name')}.{os.getenv('REPL_OWNER', 'your-username')}.repl.co"
    
    try:
        await bot.application.bot.set_webhook(url=f"{webhook_url}/webhook")
        logger.info(f"Webhook set to: {webhook_url}/webhook")
    except Exception as e:
        logger.error(f"Error setting webhook: {e}")

if __name__ == "__main__":
    # بدء Flask للحفاظ على البوت نشطاً
    keep_alive()
    
    # إعداد الـ Webhook
    asyncio.run(setup_webhook())
    
    # تشغيل البوت
    logger.info("Bot started successfully!")

