import os
import imaplib
import email
import re
import logging
from instagrapi import Client
from instagrapi.mixins.challenge import ChallengeChoice
from instagrapi.exceptions import ChallengeRequired
from telegram.ext import CommandHandler

logger = logging.getLogger(__name__)

INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "").strip()
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "").strip()
EMAIL_USER = os.getenv("INSTAGRAM_EMAIL", "").strip()
EMAIL_PASS = os.getenv("INSTAGRAM_EMAIL_PASS", "").strip()
SESSION_FILE = "/tmp/instagram_session.json"

cl = Client()


def get_code_from_email(username):
    if not EMAIL_USER or not EMAIL_PASS:
        return False
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")
        result, data = mail.search(None, "(UNSEEN)")
        if result != "OK": return False
        ids = data[0].split()
        for num in reversed(ids):
            result, data = mail.fetch(num, "(RFC822)")
            if result != "OK": continue
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
                return match.group(1)
        return False
    except Exception as e:
        logger.error(f"Email error: {e}")
        return False


def challenge_code_handler(username, choice):
    if choice == ChallengeChoice.EMAIL:
        return get_code_from_email(username)
    return False


def check_login() -> dict:
    if not INSTAGRAM_USERNAME or not INSTAGRAM_PASSWORD:
        return {"ok": False, "error": "INSTAGRAM_USERNAME یا INSTAGRAM_PASSWORD تنظیم نشده"}
    
    try:
        if os.path.exists(SESSION_FILE):
            cl.load_settings(SESSION_FILE)
            cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            user_info = cl.user_info(cl.user_id)
            return {
                "ok": True, 
                "username": user_info.username,
                "followers": user_info.follower_count
            }
    except ChallengeRequired:
        return {"ok": False, "error": "نیاز به تایید هویت (Challenge) داره. /insta_login بزن."}
    except Exception as e:
        logger.warning(f"Session check error: {e}")
        
    return {"ok": False, "error": "لاگین نیستی. اول /insta_login بزن."}


def instagram_login() -> dict:
    if not INSTAGRAM_USERNAME or not INSTAGRAM_PASSWORD:
        return {"ok": False, "error": "یوزرنیم/پسورد ست نشده"}

    cl.challenge_code_handler = challenge_code_handler

    try:
        if os.path.exists(SESSION_FILE):
            try:
                cl.load_settings(SESSION_FILE)
                cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
                return {"ok": True}
            except Exception as e:
                logger.warning(f"Session old: {e}")

        cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
        cl.dump_settings(SESSION_FILE)
        return {"ok": True}
    except ChallengeRequired:
        try:
            cl.challenge_resolve(cl.last_json)
            cl.dump_settings(SESSION_FILE)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"Challenge failed: {str(e)}"}
    except Exception as e:
        logger.error(f"Login error: {e}")
        return {"ok": False, "error": str(e)}


def post_photo(image_path: str, caption: str) -> str:
    if not check_login().get("ok"):
        if not instagram_login().get("ok"):
            raise Exception("لاگین به اینستاگرام ممکن نشد")
    media = cl.photo_upload(image_path, caption)
    return str(media.pk)


async def insta_login_command(update, context):
    if update.effective_user.id != int(os.getenv("ADMIN_ID", "0")):
        return
    await update.message.reply_text("⏳ در حال لاگین...")
    res = instagram_login()
    if res["ok"]:
        await update.message.reply_text("✅ اینستاگرام لاگین شد!")
    else:
        await update.message.reply_text(f"❌ خطا: {res.get('error', '')[:300]}")


def register_instagram_handlers(app):
    app.add_handler(CommandHandler("insta_login", insta_login_command))
