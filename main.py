import os
import json
import time
import logging
import threading
from io import BytesIO
from urllib.parse import quote

import requests
import google.generativeai as genai
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode

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

# ---------- متغیرهای محیطی ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "@KriptoKlik")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
POST_INTERVAL_SECONDS = int(os.getenv("POST_INTERVAL_SECONDS", "21600"))

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
MODEL = genai.GenerativeModel("gemini-1.5-flash") if GEMINI_API_KEY else None

AUTO_JOB_NAME = "auto_post"


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


# ---------- ۱. قیمت لحظه‌ای با retry ----------
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
                logger.warning("CoinGecko rate limit (429). Waiting %ds...", wait)
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
        raise RuntimeError(f"CoinGecko در دسترس نیست: {last_error}")

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
            f"۲۴ ساعت: {arrow_24} {change_24h:+.2f}% | "
            f"۷ روز: {arrow_7} {change_7d:+.2f}%"
        )

    return data, "\n".join(lines)


# ---------- ۲. نمودار با retry ----------
def build_chart_url(coin_id: str) -> str:
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": "7"}

    last_error = None
    prices = None
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                wait = 10 * (attempt + 1)
                logger.warning("CoinGecko chart rate limit (429). Waiting %ds...", wait)
                time.sleep(wait)
                last_error = "rate limit"
                continue
            r.raise_for_status()
            prices = r.json()["prices"]
            break
        except requests.exceptions.HTTPError as e:
            last_error = e
            time.sleep(5)
    if prices is None:
        raise RuntimeError(f"CoinGecko chart در دسترس نیست: {last_error}")

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
    return (
        f"https://quickchart.io/chart?c={encoded}"
        f"&w=800&h=400&bg=0f0f0f&f=png"
    )


# ---------- ۳. متن با Gemini ----------
def generate_post_text(market_summary: str) -> str:
    if not MODEL:
        return (
            "🚀 وضعیت لحظه‌ای بازار کریپتو\n\n"
            f"{market_summary}\n\n"
            "🔔 برای تحلیل‌های روزانه ما رو دنبال کنید: @KriptoKlik\n"
            "#کریپتو #بیت_کوین #اتریوم #ترید #سرمایه_گذاری"
        )

    prompt = f"""تو یک تولیدکننده محتوای فارسی برای یک کانال تلگرامی کریپتو به اسم «کریپتو کلیک» هستی.
وظیفه‌ت اینه که بر اساس داده‌های زیر یه پست جذاب، صمیمی و خودمونی بنویسی.

داده‌های بازار:
{market_summary}

شرایط پست:
- لحن: صمیمی و خودمونی، مثل یه دوست که داره تحلیل می‌کنه
- فارسی روان، بدون کلمه‌های انگلیسی اضافه
- شروع با یه جمله‌ی قلاب‌دار
- ۱ تا ۲ جمله تحلیل کوتاه از وضعیت
- حداقل ۵ تا ۸ ایموجی مرتبط در کل متن
- حتماً این هشتگ‌ها آخرش باشه: #کریپتو #بیت_کوین #اتریوم #ترید #سرمایه_گذاری
- حتماً این CTA آخرش باشه: «🔔 برای تحلیل‌های روزانه ما رو دنبال کنید: @KriptoKlik»
- کل متن بین ۳ تا ۵ خط باشه (کوتاه و خوانا)
- از Markdown یا کاراکترهای عجیب استفاده نکن

فقط خود متن پست رو برگردون، هیچ توضیح اضافه‌ای نده."""

    resp = MODEL.generate_content(prompt)
    return resp.text.strip()


# ---------- ۴. ساخت و ارسال ----------
async def build_and_send_post(context: ContextTypes.DEFAULT_TYPE):
    market_data, summary = fetch_crypto_data()
    logger.info("Market data fetched successfully.")

    post_text = generate_post_text(summary)
    logger.info("Post text generated. Length: %d chars", len(post_text))

    # ⏳ تاخیر بین دو درخواست CoinGecko
    time.sleep(8)

    chart_url = build_chart_url("bitcoin")
    img_resp = requests.get(chart_url, timeout=60)
    img_resp.raise_for_status()
    logger.info("Chart downloaded. Size: %d bytes", len(img_resp.content))

    photo = BytesIO(img_resp.content)
    photo.name = "chart.png"

    try:
        await context.bot.send_photo(
            chat_id=TARGET_CHANNEL,
            photo=photo,
            caption=post_text,
        )
        logger.info("Photo sent successfully to %s", TARGET_CHANNEL)
    except Exception as e:
        logger.exception("send_photo failed: %s", e)
        raise


# ---------- دستورات ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 👋\n\n"
        "دستورات:\n"
        "/generate_post — ارسال فوری پست هوشمند\n"
        "/set_channel @name — تغییر کانال مقصد\n"
        "/show_channel — نمایش کانال فعلی\n"
        "/auto_on — روشن کردن ارسال خودکار هر ۶ ساعت\n"
        "/auto_off — خاموش کردن\n"
        "/auto_status — وضعیت"
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
    await update.message.reply_text("⏳ در حال ساخت پست هوشمند... (۲۰–۴۰ ثانیه)")
    try:
        await build_and_send_post(context)
        await update.message.reply_text("✅ پست هوشمند با موفقیت منتشر شد.")
    except Exception as e:
        logger.exception("Error in generate_post")
        await update.message.reply_text(f"❌ خطا: {type(e).__name__}: {str(e)[:200]}")


# ---------- زمان‌بند ----------
async def auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️ شما اجازه این کار را ندارید.")
        return
    for job in context.job_queue.get_jobs_by_name(AUTO_JOB_NAME):
        job.schedule_removal()
    context.job_queue.run_repeating(
        auto_post_job,
        interval=POST_INTERVAL_SECONDS,
        first=POST_INTERVAL_SECONDS,
        name=AUTO_JOB_NAME,
    )
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
    for job in jobs:
        job.schedule_removal()
    await update.message.reply_text("🛑 ارسال خودکار خاموش شد.")


async def auto_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = context.job_queue.get_jobs_by_name(AUTO_JOB_NAME)
    if jobs and jobs[0].next_t:
        nxt = jobs[0].next_t.strftime("%Y-%m-%d %H:%M UTC")
        await update.message.reply_text(f"🟢 فعال — اجرای بعدی: {nxt}")
    else:
        await update.message.reply_text("🔴 خاموش است.")


async def auto_post_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Auto-post job running...")
    try:
        await build_and_send_post(context)
        logger.info("Auto-post sent.")
    except Exception:
        logger.exception("Auto-post failed")


# ---------- راه‌اندازی ----------
def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN تنظیم نشده.")
    if not GEMINI_API_KEY:
        logger.warning("⚠️ GEMINI_API_KEY تنظیم نشده — متن پیش‌فرض استفاده می‌شود.")

    threading.Thread(target=run_web_server, daemon=True).start()

    if not ADMIN_ID:
        logger.warning("⚠️ ADMIN_ID تنظیم نشده.")

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
