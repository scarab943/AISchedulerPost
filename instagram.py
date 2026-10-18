import os
import logging
from instagrapi import Client
from telegram.ext import CommandHandler

logger = logging.getLogger(__name__)

INSTAGRAM_SESSIONID = os.getenv("INSTAGRAM_SESSIONID", "").strip()

cl = Client()


def check_login() -> dict:
    """بررسی وضعیت لاگین با sessionid."""
    if not INSTAGRAM_SESSIONID:
        return {"ok": False, "error": "INSTAGRAM_SESSIONID تنظیم نشده"}

    try:
        # دیکد کردن sessionid (چون از مرورگر کپی شده و URL-encoded هست)
        import urllib.parse
        decoded_session = urllib.parse.unquote(INSTAGRAM_SESSIONID)
        
        # استفاده از sessionid برای لاگین
        cl.login_by_sessionid(decoded_session)
        user_info = cl.user_info(cl.user_id)
        return {
            "ok": True, 
            "username": user_info.username,
            "followers": user_info.follower_count
        }
    except Exception as e:
        logger.error(f"Session login error: {e}")
        return {"ok": False, "error": str(e)}


def instagram_login() -> dict:
    return check_login()


def post_photo(image_path: str, caption: str) -> str:
    if not check_login().get("ok"):
        raise Exception("لاگین به اینستاگرام ممکن نشد. sessionid رو چک کن.")

    media = cl.photo_upload(image_path, caption)
    return str(media.pk)


async def insta_login_command(update, context):
    if update.effective_user.id != int(os.getenv("ADMIN_ID", "0")):
        return
    await update.message.reply_text("⏳ در حال بررسی sessionid...")
    res = check_login()
    if res["ok"]:
        await update.message.reply_text(f"✅ لاگین هستی (@{res['username']})")
    else:
        await update.message.reply_text(f"❌ خطا: {res.get('error', '')[:300]}")


def register_instagram_handlers(app):
    app.add_handler(CommandHandler("insta_login", insta_login_command))
