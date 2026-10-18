"""
ماژول اینستاگرام - نسخه‌ی پایدار با خواندن کد از ایمیل
"""
import os
import imaplib
import email
import re
import logging
from instagrapi import Client
from instagrapi.mixins.challenge import ChallengeChoice
from instagrapi.exceptions import LoginRequired
from telegram.ext import CommandHandler

logger = logging.getLogger(__name__)

# اطلاعات از متغیرهای محیطی Railway
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "").strip()
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "").strip()
EMAIL_USER = os.getenv("INSTAGRAM_EMAIL", "").strip()
EMAIL_PASS = os.getenv("INSTAGRAM_EMAIL_PASS", "").strip()
SESSION_FILE = "/tmp/instagram_session.json"

cl = Client()


def get_code_from_email(username):
    """اتصال به ایمیل و استخراج کد تایید اینستاگرام."""
    if not EMAIL_USER or not EMAIL_PASS:
        logger.error("EMAIL_USER یا EMAIL_PASS تنظیم نشده")
        return False
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")

        result, data = mail.search(None, "(UNSEEN)")
        if result != "OK":
            return False

        ids = data[0].split()
        for num in reversed(ids):
            result, data = mail.fetch(num, "(RFC822)")
            if result != "OK":
                continue

            msg = email.message_from_bytes(data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode(errors="ignore")

            match = re.search(r'\b(\d{6})\b', body)
            if match:
                mail.store(num, '+FLAGS', '\\Seen')
                logger.info("کد تایید از ایمیل استخراج شد")
                return match.group(1)

        return False
    except Exception as e:
        logger.error(f"خطا در خواندن ایمیل: {e}")
        return False


def challenge_code_handler(username, choice):
    if choice == ChallengeChoice.EMAIL:
        return get_code_from_email(username)
    return False


def check_login() -> bool:
    """بررسی وضعیت لاگین اینستاگرام."""
    if not INSTAGRAM_USERNAME or not INSTAGRAM_PASSWORD:
        return False
    try:
        if os.path.exists(SESSION_FILE):
            cl.load_settings(SESSION_FILE)
            cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            return True
    except LoginRequired:
        pass
    except Exception as e:
        logger.warning(f"Session load error: {e}")
    return False


def instagram_login() -> bool:
    """لاگین به اینستاگرام با پشتیبانی از کد تایید."""
    if not INSTAGRAM_USERNAME or not INSTAGRAM_PASSWORD:
        logger.error("INSTAGRAM_USERNAME یا INSTAGRAM_PASSWORD تنظیم نشده")
        return False

    cl.challenge_code_handler = challenge_code_handler

    try:
        if os.path.exists(SESSION_FILE):
            try:
                cl.load_settings(SESSION_FILE)
                cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
                logger.info("اینستاگرام با session قبلی لاگین شد")
                return True
            except Exception as e:
                logger.warning(f"Session قدیمی نامعتبر: {e}")

        cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
        cl.dump_settings(SESSION_FILE)
        logger.info("اینستاگرام با موفقیت لاگین شد")
        return True
    except Exception as e:
        logger.error(f"خطای لاگین اینستاگرام: {e}")
        return False


def post_to_instagram(image_url: str, caption: str) -> bool:
    """پست عکس در اینستاگرام."""
    if not check_login():
        if not instagram_login():
            return False

    try:
        cl.photo_upload(image_url, caption)
        logger.info("پست در اینستاگرام منتشر شد")
        return True
    except Exception as e:
        logger.error(f"خطای پست اینستاگرام: {e}")
        return False


# ============ دستورات تلگرام ============
async def insta_login_command(update, context):
    if update.effective_user.id != int(os.getenv("ADMIN_ID", "0")):
        await update.message.reply_text("⛔️")
        return
    await update.message.reply_text("⏳ در حال لاگین به اینستاگرام...")
    if instagram_login():
        await update.message.reply_text("✅ اینستاگرام لاگین شد!")
    else:
        await update.message.reply_text("❌ لاگین ناموفق. لاگ Railway رو چک کن.")


async def insta_status_command(update, context):
    if update.effective_user.id != int(os.getenv("ADMIN_ID", "0")):
        await update.message.reply_text("⛔️")
        return
    if check_login():
        await update.message.reply_text(f"✅ لاگین هستی ({INSTAGRAM_USERNAME})")
    else:
        await update.message.reply_text("❌ لاگین نیستی. /insta_login بزن.")


def register_instagram_handlers(app):
    """این تابع توی main.py صدا زده می‌شه."""
    app.add_handler(CommandHandler("insta_login", insta_login_command))
    app.add_handler(CommandHandler("insta_status", insta_status_command))
    logger.info("Instagram handlers registered.")