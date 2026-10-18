"""
ماژول دوبله ویدیو با Colab + انتشار در اینستاگرام
دستورات: /dub, /set_webhook, /check_colab, /check_instagram, /post_ig
"""

import os
import time
import asyncio
import logging
import requests
from telegram import Update
from telegram.ext import ContextTypes, Application, CommandHandler

logger = logging.getLogger(__name__)

COLAB_URL = os.getenv("COLAB_URL", "").strip()


def get_admin_id() -> int:
    return int(os.getenv("ADMIN_ID", "0"))


def is_admin(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == get_admin_id()


# ═══════════════════════════════════════════════════
# دوبله
# ═══════════════════════════════════════════════════
async def dub_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global COLAB_URL
    if not is_admin(update):
        await update.message.reply_text("⛔️ شما اجازه این کار را ندارید.")
        return

    if not COLAB_URL:
        await update.message.reply_text(
            "❌ آدرس Colab تنظیم نشده!\n"
            "اول توی Colab سرور رو راه‌اندازی کن، بعد اینجا بزن:\n"
            "/set_webhook https://xxx.trycloudflare.com"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "مثال:\n/dub https://www.youtube.com/watch?v=XXXXX"
        )
        return

    youtube_url = context.args[0].strip()
    await update.message.reply_text(
        f"⏳ شروع دوبله...\n"
        f"🔗 {youtube_url}\n\n"
        f"این کار ۲ تا ۵ دقیقه طول می‌کشه."
    )

    try:
        r = requests.post(
            f"{COLAB_URL}/dub",
            json={"url": youtube_url},
            timeout=30
        )
        r.raise_for_status()
        job_id = r.json()["job_id"]
    except Exception as e:
        logger.exception("Error contacting Colab")
        await update.message.reply_text(f"❌ خطا در ارتباط با Colab: {str(e)[:200]}")
        return

    max_wait = 15 * 60
    start_time = time.time()
    last_status = ""

    while time.time() - start_time < max_wait:
        try:
            sr = requests.get(f"{COLAB_URL}/status/{job_id}", timeout=30)
            sr.raise_for_status()
            data = sr.json()
            status = data.get("status", "unknown")

            if status != last_status:
                logger.info("Job %s status: %s", job_id, status)
                last_status = status

            if status == "done":
                await update.message.reply_text(
                    f"✅ دوبله کامل شد!\n"
                    f"📊 حجم: {data.get('size_mb', 0):.1f} MB\n"
                    f"📝 ترجمه:\n{data.get('translation', '')[:500]}"
                )

                video_path = data.get("video_path", "")
                if not video_path:
                    await update.message.reply_text("❌ مسیر ویدیو پیدا نشد.")
                    return

                await update.message.reply_text("📥 در حال دانلود ویدیو از Colab...")
                try:
                    dl = requests.get(
                        f"{COLAB_URL}/download/{job_id}",
                        timeout=300,
                        stream=True
                    )
                    dl.raise_for_status()

                    local_path = f"/tmp/dubbed_{job_id}.mp4"
                    with open(local_path, "wb") as f:
                        for chunk in dl.iter_content(chunk_size=1024*1024):
                            if chunk:
                                f.write(chunk)

                    file_size = os.path.getsize(local_path) / (1024 * 1024)
                    logger.info("Video downloaded: %.1f MB", file_size)

                    with open(local_path, "rb") as f:
                        await context.bot.send_video(
                            chat_id=get_admin_id(),
                            video=f,
                            caption=(
                                f"🎬 ویدیوی دوبله‌شده آماده است!\n\n"
                                f"📊 حجم: {file_size:.1f} MB\n\n"
                                f"━━━━━━━━━━━━━━━\n"
                                f"🔹 ۱. ویدیو رو دانلود کن\n"
                                f"🔹 ۲. توی یوتیوب آپلود کن\n"
                                f"🔹 ۳. لینک رو برام بفرست:\n"
                                f"/set_youtube_link https://youtu.be/XXXX"
                            ),
                            supports_streaming=True,
                        )
                    logger.info("Video sent to admin.")

                    try:
                        os.remove(local_path)
                    except Exception:
                        pass

                except Exception as e:
                    logger.exception("Error downloading video")
                    await update.message.reply_text(f"❌ خطا در دانلود: {str(e)[:200]}")

                return

            if status == "failed":
                await update.message.reply_text(
                    f"❌ دوبله ناموفق:\n{data.get('error', 'خطای ناشناخته')[:300]}"
                )
                return

        except Exception as e:
            logger.warning("Status check error: %s", e)

        await asyncio.sleep(30)

    await update.message.reply_text("⏰ زمان انتظار تموم شد.")


async def set_webhook_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global COLAB_URL
    if not is_admin(update):
        await update.message.reply_text("⛔️ شما اجازه این کار را ندارید.")
        return

    if not context.args:
        await update.message.reply_text(
            "مثال:\n/set_webhook https://xxx.trycloudflare.com"
        )
        return

    url = context.args[0].strip().rstrip("/")

    try:
        r = requests.get(f"{url}/", timeout=15)
        if r.status_code == 200:
            COLAB_URL = url
            await update.message.reply_text(
                f"✅ آدرس Colab تنظیم شد:\n{COLAB_URL}\n\n"
                f"حالا می‌تونی از /dub استفاده کنی."
            )
        else:
            await update.message.reply_text(f"❌ خطا: سرور پاسخ {r.status_code} داد.")
    except Exception as e:
        await update.message.reply_text(f"❌ نمی‌تونم به Colab وصل بشم: {str(e)[:200]}")


async def check_colab_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️")
        return
    if not COLAB_URL:
        await update.message.reply_text("❌ آدرس Colab تنظیم نشده.")
        return
    try:
        r = requests.get(f"{COLAB_URL}/", timeout=15)
        await update.message.reply_text(
            f"✅ Colab فعاله\n"
            f"🔗 {COLAB_URL}\n"
            f"📊 پاسخ: {r.status_code}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Colab در دسترس نیست: {str(e)[:200]}")


# ═══════════════════════════════════════════════════
# اینستاگرام
# ═══════════════════════════════════════════════════
async def check_instagram_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️")
        return
    await update.message.reply_text("⏳ در حال بررسی اینستاگرام...")
    try:
        from instagram import check_login
        # تابع check_login در instagram.py یک دیکشنری برمی‌گرداند
        result = check_login()
        if isinstance(result, dict) and result.get("ok"):
            await update.message.reply_text(
                f"✅ اینستاگرام فعاله\n"
                f"👤 یوزرنیم: @{result.get('username')}\n"
                f"👥 دنبال‌کننده: {result.get('followers')}"
            )
        elif isinstance(result, dict):
            await update.message.reply_text(f"❌ خطا:\n{str(result.get('error'))[:300]}")
        else:
            await update.message.reply_text(f"❌ وضعیت نامشخص: {result}")
    except Exception as e:
        logger.exception("Error in check_instagram")
        await update.message.reply_text(f"❌ خطا: {str(e)[:300]}")


async def post_to_instagram_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️")
        return
    if not context.args:
        await update.message.reply_text(
            "مثال:\n/post_ig https://youtu.be/XXXX"
        )
        return

    video_url = context.args[0].strip()
    await update.message.reply_text("⏳ در حال آماده‌سازی پست اینستاگرام...")

    try:
        from instagram import post_photo

        vid_id = video_url.split("v=")[-1].split("/")[-1].split("?")[0]
        thumb_url = f"https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg"

        r = requests.get(thumb_url, timeout=30)
        r.raise_for_status()

        tmp_path = f"/tmp/ig_thumb_{vid_id}.jpg"
        with open(tmp_path, "wb") as f:
            f.write(r.content)

        caption = (
            f"🎬 ویدیوی جدید ما منتشر شد!\n\n"
            f"توی این ویدیو تحلیل کامل بازار رو گفتم.\n\n"
            f"🔗 لینک ویدیو توی بایو ما\n\n"
            f"#کریپتو #بیت_کوین #اتریوم #ترید #سرمایه_گذاری"
        )

        media_id = post_photo(tmp_path, caption)

        try:
            os.remove(tmp_path)
        except Exception:
            pass

        await update.message.reply_text(
            f"✅ پست اینستاگرام منتشر شد!\n🆔 {media_id}"
        )
    except Exception as e:
        logger.exception("Error in post_to_instagram")
        await update.message.reply_text(f"❌ خطا: {str(e)[:300]}")


# ═══════════════════════════════════════════════════
# ثبت handlerها
# ═══════════════════════════════════════════════════
def register_dubbing_handlers(app: Application):
    app.add_handler(CommandHandler("dub", dub_video_command))
    app.add_handler(CommandHandler("set_webhook", set_webhook_command))
    app.add_handler(CommandHandler("check_colab", check_colab_command))
    app.add_handler(CommandHandler("check_instagram", check_instagram_command))
    app.add_handler(CommandHandler("post_ig", post_to_instagram_command))
    logger.info("Dubbing handlers registered.")
