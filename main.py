import os
import logging
from io import BytesIO
from urllib.parse import quote

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "@KriptoKlik")

# فاصله زمانی ارسال خودکار (به ثانیه) — ۶ ساعت = 21600
POST_INTERVAL_SECONDS = int(os.getenv("POST_INTERVAL_SECONDS", "21600"))


def is_admin(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == ADMIN_ID


async def build_and_send_post(context: ContextTypes.DEFAULT_TYPE):
    """تولید تصویر + ارسال به کانال. هم از دستور و هم از زمان‌بند استفاده میشه."""
    post_text = (
        "🚀 <b>فناوری و آینده هوش مصنوعی</b>\n\n"
        "جهان پیرامون ما با سرعت در حال تغییر است. هوش مصنوعی دیگر یک رویا نیست، "
        "بلکه ابزاری قدرتمند برای ساخت آینده‌ای بهتر است.\n\n"
        "✨ مسیر موفقیت خود را هم‌اکنون بسازید!"
    )

    image_prompt = "A modern futuristic workspace, high quality, digital art, cyberpunk style"
    encoded_prompt = quote(image_prompt, safe="")
    image_url = (
        f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        f"?width=1024&height=1024&nologo=true"
    )

    r = requests.get(image_url, timeout=90)
    if r.status_code != 200 or len(r.content) < 500:
        raise RuntimeError(f"تصویر دریافت نشد (کد {r.status_code})")

    photo = BytesIO(r.content)
    photo.name = "post.jpg"

    await context.bot.send_photo(
        chat_id=TARGET_CHANNEL,
        photo=photo,
        caption=post_text,
        parse_mode=ParseMode.HTML,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 👋\n\n"
        "دستورات:\n"
        "/generate_post — ارسال فوری پست به کانال\n"
        "/set_channel @name — تغییر کانال مقصد\n"
        "/show_channel — نمایش کانال فعلی\n"
        "/auto_on — روشن کردن ارسال خودکار هر ۶ ساعت\n"
        "/auto_off — خاموش کردن ارسال خودکار\n"
        "/auto_status — وضعیت ارسال خودکار"
    )


async def show_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"کانال فعلی: {TARGET_CHANNEL}")


async def set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHANNEL
    if not is_admin(update):
        await update.message.reply_text("⛔️ شما اجازه این کار را ندارید.")
        return
    if not context.args:
        await update.message.reply_text("مثال:\n/set_channel @mychannel")
        return
    channel = context.args[0].strip()
    if not (channel.startswith("@") or channel.startswith("-100")):
        await update.message.reply_text("❌ فرمت کانال نامعتبر است.")
        return
    TARGET_CHANNEL = channel
    await update.message.reply_text(f"✅ کانال به {TARGET_CHANNEL} تغییر یافت.")


async def generate_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️ شما اجازه این کار را ندارید.")
        return

    await update.message.reply_text("⏳ در حال تولید پست...")
    try:
        await build_and_send_post(context)
        await update.message.reply_text("✅ پست با موفقیت منتشر شد.")
    except requests.exceptions.Timeout:
        await update.message.reply_text("❌ زمان انتظار تمام شد. دوباره تلاش کن.")
    except Exception:
        logger.exception("Error generating post")
        await update.message.reply_text("❌ خطای غیرمنتظره‌ای رخ داد.")


# ----- زمان‌بند -----

AUTO_JOB_NAME = "auto_post"


async def auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️ شما اجازه این کار را ندارید.")
        return

    # اگه قبلاً فعال بوده، پاکش کن که دوتا نشه
    for job in context.job_queue.get_jobs_by_name(AUTO_JOB_NAME):
        job.schedule_removal()

    context.job_queue.run_repeating(
        auto_post_job,
        interval=POST_INTERVAL_SECONDS,
        first=POST_INTERVAL_SECONDS,   # اولین اجرا بعد از ۶ ساعت
        name=AUTO_JOB_NAME,
    )
    hours = POST_INTERVAL_SECONDS / 3600
    await update.message.reply_text(
        f"✅ ارسال خودکار روشن شد. هر {hours:g} ساعت یه پست میره توی {TARGET_CHANNEL}."
    )


async def auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️ شما اجازه این کار را ندارید.")
        return
    jobs = context.job_queue.get_jobs_by_name(AUTO_JOB_NAME)
    if not jobs:
        await update.message.reply_text("ℹ️ ارسال خودکار از قبل خاموش بود.")
        return
    for job in jobs:
        job.schedule_removal()
    await update.message.reply_text("🛑 ارسال خودکار خاموش شد.")


async def auto_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = context.job_queue.get_jobs_by_name(AUTO_JOB_NAME)
    if jobs and jobs[0].next_t:
        nxt = jobs[0].next_t.strftime("%Y-%m-%d %H:%M UTC")
        await update.message.reply_text(f"🟢 فعال — اجرای بعدی: {nxt}")
    else:
        await update.message.reply_text("🔴 ارسال خودکار خاموش است.")


async def auto_post_job(context: ContextTypes.DEFAULT_TYPE):
    """این تابع توسط JobQueue هر ۶ ساعت صدا زده میشه."""
    logger.info("Auto-post job running...")
    try:
        await build_and_send_post(context)
        logger.info("Auto-post sent to %s", TARGET_CHANNEL)
    except Exception:
        logger.exception("Auto-post failed")


# ----- راه‌اندازی -----

def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN تنظیم نشده است.")
    if not ADMIN_ID:
        logger.warning("⚠️ ADMIN_ID تنظیم نشده — دستورات برای هیچ‌کس اجرا نمی‌شوند.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("show_channel", show_channel))
    app.add_handler(CommandHandler("set_channel", set_channel))
    app.add_handler(CommandHandler("generate_post", generate_post))
    app.add_handler(CommandHandler("auto_on", auto_on))
    app.add_handler(CommandHandler("auto_off", auto_off))
    app.add_handler(CommandHandler("auto_status", auto_status))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()