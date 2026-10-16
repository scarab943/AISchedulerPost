"""
ماژول انتشار در اینستاگرام با Instagrapi
"""

import os
import logging
from pathlib import Path
from instagrapi import Client

logger = logging.getLogger(__name__)

IG_USERNAME = os.getenv("INSTAGRAM_USERNAME", "").strip()
IG_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "").strip()
IG_SESSION_FILE = "/tmp/ig_session.json"

_client = None


def get_client():
    """کلاینت اینستاگرام رو می‌سازه یا از session ذخیره‌شده بازیابی می‌کنه."""
    global _client

    if _client is not None:
        return _client

    if not IG_USERNAME or not IG_PASSWORD:
        raise RuntimeError("INSTAGRAM_USERNAME یا INSTAGRAM_PASSWORD تنظیم نشده")

    cl = Client()
    cl.delay_range = [1, 3]  # تاخیر انسانی بین درخواست‌ها

    # اگه session ذخیره شده داریم، ازش استفاده کن
    if Path(IG_SESSION_FILE).exists():
        try:
            cl.load_settings(IG_SESSION_FILE)
            cl.login(IG_USERNAME, IG_PASSWORD)
            cl.get_timeline_feed()  # تست session
            logger.info("✅ Instagram session restored")
            _client = cl
            return cl
        except Exception as e:
            logger.warning("Session restore failed: %s. Trying fresh login.", e)

    # لاگین تازه
    try:
        cl.login(IG_USERNAME, IG_PASSWORD)
        cl.dump_settings(IG_SESSION_FILE)
        logger.info("✅ Instagram login successful")
        _client = cl
        return cl
    except Exception as e:
        logger.error("Instagram login failed: %s", e)
        raise


def post_photo(image_path: str, caption: str) -> str:
    """
    یه عکس + کپشن توی اینستاگرام پست می‌کنه.
    خروجی: media pk (شناسه پست)
    """
    cl = get_client()

    # آپلود عکس
    media = cl.photo_upload(
        path=image_path,
        caption=caption,
    )

    logger.info("✅ Instagram post published: %s", media.pk)
    return str(media.pk)


def check_login() -> dict:
    """وضعیت لاگین رو برمی‌گردونه."""
    try:
        cl = get_client()
        user_info = cl.account_info()
        return {
            "ok": True,
            "username": user_info.username,
            "full_name": user_info.full_name,
            "followers": user_info.follower_count,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
