import os
import json
import time
import logging
import threading
from io import BytesIO
from urllib.parse import quote

import requests
from google import genai
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import TelegramError

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# خاموش کردن لاگ‌های پرحجم که توکن رو لو می‌دن
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logging.getLogger("telegram.request").setLevel(logging.WARNING)
logging.getLogger("google").setLevel(logging.WARNING)

# ---------- متغیرهای محیطی ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "@KriptoKlik").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
POST_INTERVAL_SECONDS = int(os.getenv("POST_INTERVAL_SECONDS", "21600"))
AUTO_POST_ENABLED = os.getenv("AUTO_POST_ENABLED", "true").lower() == "true"

GEMINI_MODEL = "gemini-flash-latest"
GEMINI_CLIENT = None
if GEMINI_API_KEY:
    GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)

AUTO_JOB_NAME = "auto_post"
CAPTION_LIMIT = 1000

# فلگ runtime که وضعیت فعلی رو نشون می‌ده
auto_is_running = False


def is_admin(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == ADMIN_ID


# ---------- وب‌سرور ----------
async def health(request):
    return web.Response(text="OK")


def run_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    port = int(os.getenv("PORT", "8080"))
    logger.info("Web server starting on port %s", port)
    web.run_app(app, host="0.0.0.0", port=port, print=None)


# ---------- قیمت ----------
def fetch_crypto_data():
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "ids": "bitcoin,ethereum",
        "order": "market_cap_desc",
        "sparkline": "false",
        "price_change_percentage": "24h,7d",
    }
    last_error = None
    data = None
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                wait = 10 * (attempt + 1)
                logger.warning("CoinGecko 429. Waiting %ds...", wait)
                time.sleep(wait)
                last_error = "rate limit"
                continue
            r.raise_for_status()
            data = r.json()
            break
        except requests.exceptions.HTTPError as e:
            last_error = e
            time.sleep(5)
    if data is None:
        raise RuntimeError(f"CoinGecko error: {last_error}")

    lines = []
    for coin in data:
        name = coin["name"]
        symbol = coin["symbol"].upper()
        price = coin["current_price"]
        change_24h = coin.get("price_change_percentage_24h") or 0
        change_7d = coin.get("price_change_percentage_7d_in_currency") or 0
        arrow_24 = "🔺" if change_24h >= 0 else "🔻"
        arrow_7 = "🔺" if change_7d >= 0 else "🔻"
        lines.append(
            f"• {name} ({symbol}): ${price:,.0f} | "
            f"۲۴س: {arrow_24} {change_24h:+.2f}% | "
            f"۷ر: {arrow_7} {change_7d:+.2f}%"
        )
    return data, "\n".join(lines)


# ---------- نمودار ----------
def build_chart_url(coin_id: str) -> str:
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": "7"}
    last_error = None
    prices = None
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                last_error = "rate limit"
                continue
            r.raise_for_status()
            prices = r.json()["prices"]
            break
        except requests.exceptions.HTTPError as e:
            last_error = e
            time.sleep(5)
    if prices is None:
        raise RuntimeError(f"CoinGecko chart error: {last_error}")

    step = max(1, len(prices) // 14)
    sampled = prices[::step][:14]
    labels = [f"{i+1}" for i in range(len(sampled))]
    data_points = [round(p[1], 2) for p in sampled]
    color = "#f7931a" if coin_id == "bitcoin" else "#627eea"

    chart_config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": f"{coin_id.upper()} Price (7d)",
                "data": data_points,
                "borderColor": color,
                "backgroundColor": color + "33",
                "fill": True,
                "tension": 0.4,
                "pointRadius": 0,
            }]
        },
        "options": {
            "legend": {"display": False},
            "scales": {
                "xAxes": [{"gridLines": {"display": False}}],
                "yAxes": [{"gridLines": {"color": "#333"}}],
            }
        }
    }
    encoded = quote(json.dumps(chart_config), safe="")
    return f"https://quickchart.io/chart?c={encoded}&w=800&h=400&bg=0f0f0f&f=png"


# ---------- متن با Gemini ----------
def generate_post_text(market_summary: str) -> str:
    fallback = (
        "🚀 وضعیت لحظه‌ای بازار کریپتو\n\n"
        f"{market_summary}\n\n"
        "🔔 برای تحلیل‌های روزانه ما رو دنبال کنید: @KriptoKlik\n"
        "#کریپتو #بیت_کوین #اتریوم #ترید #سرمایه_گذاری"
    )
    if not GEMINI_CLIENT:
        return fallback

    prompt = f"""تو یک تولیدکننده محتوای فارسی برای یک کانال تلگرامی کریپتو به اسم «کریپتو کلیک» هستی.
بر اساس داده‌های زیر یه پست جذاب، صمیمی و خودمونی بنویس.

داده‌های بازار:
{market_summary}

شرایط:
- لحن صمیمی و خودمونی
- فارسی روان
- شروع با یه جمله‌ی قلاب‌دار
- ۱ تا ۲ جمله تحلیل کوتاه
- ۵ تا ۸ ایموجی
- بین ۳ تا ۵ خط (کوتاه!)
- آخرش حتماً اینا باشه:
🔔 برای تحلیل‌های روزانه ما رو دنبال کنید: @KriptoKlik
#کریپتو #بیت_کوین #اتریوم #ترید #سرمایه_گذاری

فقط خود متن پست رو برگردون، هیچ توضیح اضافه‌ای نده. حتماً کمتر از ۸۰۰ کاراکتر باشه."""

    try:
        response = GEMINI_CLIENT.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        text = response.text.strip()
    except Exception as e:
        logger.error("Gemini error: %s: %s", type(e).__name__, str(e)[:300])
        return fallback

    if len(text) > CAPTION_LIMIT:
        logger.warning("Caption too long (%d). Truncating...", len(text))
        text = text[:CAPTION_LIMIT - 3] + "..."
    return text


# ---------- ساخت و ارسال ----------
async def build_and_send_post(context: ContextTypes.DEFAULT_TYPE):
    logger.info("STEP 1: fetching market data...")
    market_data, summary = fetch_crypto_data()
    logger.info("STEP 1 done.")

    logger.info("STEP 2: generating text with Gemini...")
    post_text = generate_post_text(summary)
    logger.info("STEP 2 done. Caption length: %d chars", len(post_text))

    logger.info("STEP 3: waiting 8s...")
    time.sleep(8)

    logger.info("STEP 4: fetching chart...")
    chart_url = build_chart_url("bitcoin")
    img_resp = requests.get(chart_url, timeout=60)
    img_resp.raise_for_status()
    logger.info("STEP 4 done. Size: %d bytes", len(img_resp.content))

    photo = BytesIO(img_resp.content)
    photo.name = "chart.png"

    logger.info("STEP 5: sending to %s ...", TARGET_CHANNEL)
    try:
        await context.bot.send_photo(
            chat_id=TARGET_CHANNEL,
            photo=photo,
            caption=post_text,
        )
        logger.info("STEP 5 done. Photo sent successfully.")
        return
    except TelegramError as e:
        logger.error("send_photo failed: %s: %s", type(e).__name__, str(e))
        logger.warning("Falling back to text-only message...")

    try:
        await context.bot.send_message(
            chat_id=TARGET_CHANNEL,
            text=post_text,
        )
        logger.info("Fallback text sent successfully.")
    except TelegramError as e:
        logger.error("send_message also failed: %s: %s", type(e).__name__, str(e))
        raise


# ---------- فعال‌سازی / غیرفعال‌سازی خودکار ----------
def schedule_auto_job(app):
    """تایمر ارسال خودکار رو با فاصله POST_INTERVAL_SECONDS تنظیم می‌کنه."""
    global auto_is_running
    job_queue = app.job_queue
    for job in job_queue.get_jobs_by_name(AUTO_JOB_NAME):
        job.schedule_removal()
    job_queue.run_repeating(
        auto_post_job,
        interval=POST_INTERVAL_SECONDS,
        first=POST_INTERVAL_SECONDS,
        name=AUTO_JOB_NAME,
    )
    auto_is_running = True
    logger.info(
        "Auto-post scheduled every %d seconds (%.1f hours).",
        POST_INTERVAL_SECONDS,
        POST_INTERVAL_SECONDS / 3600,
    )


def cancel_auto_job(app):
    global auto_is_running
    for job in app.job_queue.get_jobs_by_name(AUTO_JOB_NAME):
        job.schedule_removal()
    auto_is_running = False
    logger.info("Auto-post cancelled.")


# ---------- دستورات ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 👋\n\n"
        "/generate_post — ارسال فوری پست\n"
        "/set_channel @name — تغییر کانال\n"
        "/show_channel — نمایش کانال\n"
        "/debug — اطلاعات عیب‌یابی\n"
        "/auto_on — روشن کردن ارسال خودکار\n"
        "/auto_off — خاموش کردن\n"
        "/auto_status — وضعیت"
    )


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️ شما اجازه این کار را ندارید.")
        return
    jobs = context.job_queue.get_jobs_by_name(AUTO_JOB_NAME)
    next_run = jobs[0].next_t.strftime("%Y-%m-%d %H:%M UTC") if jobs and jobs[0].next_t else "—"
    msg = (
        f"🔧 اطلاعات:\n\n"
        f"ADMIN_ID: {ADMIN_ID}\n"
        f"TARGET_CHANNEL: {repr(TARGET_CHANNEL)}\n"
        f"GEMINI_MODEL: {GEMINI_MODEL}\n"
        f"GEMINI: {'✅' if GEMINI_CLIENT else '❌'}\n"
        f"POST_INTERVAL: {POST_INTERVAL_SECONDS}s ({POST_INTERVAL_SECONDS // 3600}h)\n"
        f"AUTO_POST_ENABLED (env): {AUTO_POST_ENABLED}\n"
        f"Auto running: {'🟢' if jobs else '🔴'}\n"
        f"Next run: {next_run}\n"
        f"USER_ID: {update.effective_user.id}"
    )
    await update.message.reply_text(msg)


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
        await update.message.reply_text("❌ فرمت نامعتبر.")
        return
    TARGET_CHANNEL = channel
    await update.message.reply_text(f"✅ کانال به {TARGET_CHANNEL} تغییر یافت.")


async def generate_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️ شما اجازه این کار را ندارید.")
        return
    await update.message.reply_text("⏳ در حال ساخت پست هوشمند...")
    try:
        await build_and_send_post(context)
        await update.message.reply_text("✅ پست با موفقیت منتشر شد.")
    except TelegramError as e:
        logger.exception("Telegram error")
        await update.message.reply_text(f"❌ خطای تلگرام: {type(e).__name__}: {str(e)[:200]}")
    except Exception as e:
        logger.exception("General error")
        await update.message.reply_text(f"❌ خطا: {type(e).__name__}: {str(e)[:200]}")


# ---------- زمان‌بند (دستورات دستی) ----------
async def auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️ شما اجازه این کار را ندارید.")
        return
    schedule_auto_job(context.application)
    await update.message.reply_text(
        f"✅ ارسال خودکار روشن شد. هر {POST_INTERVAL_SECONDS // 3600} ساعت."
    )


async def auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️ شما اجازه این کار را ندارید.")
        return
    jobs = context.job_queue.get_jobs_by_name(AUTO_JOB_NAME)
    if not jobs:
        await update.message.reply_text("ℹ️ از قبل خاموش بود.")
        return
    cancel_auto_job(context.application)
    await update.message.reply_text("🛑 ارسال خودکار خاموش شد.")


async def auto_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = context.job_queue.get_jobs_by_name(AUTO_JOB_NAME)
    if jobs and jobs[0].next_t:
        nxt = jobs[0].next_t.strftime("%Y-%m-%d %H:%M UTC")
        await update.message.reply_text(
            f"🟢 فعال — اجرای بعدی: {nxt}\nفاصله: هر {POST_INTERVAL_SECONDS // 3600} ساعت."
        )
    else:
        await update.message.reply_text("🔴 خاموش است.")


async def auto_post_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Auto-post running...")
    try:
        await build_and_send_post(context)
        logger.info("Auto-post done.")
    except Exception:
        logger.exception("Auto-post failed")


# ---------- Post Init: بعد از راه‌اندازی، خودکار فعال کن ----------
async def post_init(app):
    if AUTO_POST_ENABLED:
        schedule_auto_job(app)
        logger.info("Auto-post auto-enabled from env (AUTO_POST_ENABLED=true).")
    else:
        logger.info("Auto-post disabled (AUTO_POST_ENABLED=false).")


# ---------- راه‌اندازی ----------
def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN تنظیم نشده.")
    if not GEMINI_CLIENT:
        logger.warning("⚠️ GEMINI_API_KEY تنظیم نشده.")

    threading.Thread(target=run_web_server, daemon=True).start()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("debug", debug))
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
