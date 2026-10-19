import os
import json
import time
import asyncio
import logging
import subprocess
from io import BytesIO
from urllib.parse import quote

import requests
import edge_tts
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

for noisy in ["httpx", "httpcore", "telegram", "telegram.ext",
              "telegram.request", "google", "urllib3", "edge_tts"]:
    logging.getLogger(noisy).setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "@KriptoKlik").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
POST_INTERVAL_SECONDS = int(os.getenv("POST_INTERVAL_SECONDS", "21600"))
AUTO_POST_ENABLED = os.getenv("AUTO_POST_ENABLED", "true").lower() == "true"
AUTO_VIDEO_ENABLED = os.getenv("AUTO_VIDEO_ENABLED", "false").lower() == "true"
AUTO_VIDEO_INTERVAL = int(os.getenv("AUTO_VIDEO_INTERVAL", "86400"))
PROXY_URL = os.getenv("PROXY_URL", "").strip()

GEMINI_MODEL = "gemini-flash-latest"
GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

PERSIAN_VOICE = os.getenv("PERSIAN_VOICE", "fa-IR-FaridNeural")
WORKSPACE = "/tmp/video_workspace"
AUTO_JOB_NAME = "auto_post"
AUTO_VIDEO_JOB_NAME = "auto_video"
CAPTION_LIMIT = 1000

# متغیرهای گلوبال برای استفاده در پست اینستاگرام
LAST_VIDEO_TRANSLATION = ""
LAST_VIDEO_PATH = ""


def is_admin(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == ADMIN_ID


async def health(request):
    return web.Response(text="OK")


def run_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    port = int(os.getenv("PORT", "8080"))
    logger.info("Web server starting on port %s", port)
    web.run_app(app, host="0.0.0.0", port=port, print=None)


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
                time.sleep(10 * (attempt + 1))
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

فقط خود متن پست رو برگردون. کمتر از ۸۰۰ کاراکتر."""

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
        text = text[:CAPTION_LIMIT - 3] + "..."
    return text


def generate_video_script(market_summary: str) -> dict:
    fallback = {
        "title": "تحلیل سریع بازار کریپتو",
        "script": (
            f"سلام دوستان! یه نگاه سریع به بازار کریپتو بندازیم. "
            f"{market_summary.replace(chr(10), '. ')} "
            "به نظر می‌رسه بازار توی یه نقطه‌ی حساس قرار داره. "
            "نظر شما چیه؟ توی کامنت‌ها بنویسید. ممنون که همراه ما هستید!"
        ),
        "youtube_title": "🎬 تحلیل سریع بازار کریپتو امروز",
        "youtube_description": (
            f"{market_summary}\n\n"
            "🔔 برای تحلیل‌های روزانه ما رو دنبال کنید: @KriptoKlik\n"
            "#کریپتو #بیت_کوین #اتریوم #ترید"
        ),
        "youtube_tags": ["کریپتو", "بیت کوین", "اتریوم", "ترید"],
    }
    if not GEMINI_CLIENT:
        return fallback

    prompt = f"""تو یه تولیدکننده محتوای فارسی برای یه کانال یوتیوب کریپتو هستی.
بر اساس داده‌های زیر یه اسکریپت کوتاه برای یه ویدیوی ۳۰ ثانیه‌ای بنویس.

داده‌های بازار:
{market_summary}

خروجی رو دقیقاً به این فرمت JSON بده (فقط JSON، هیچ متن اضافه‌ای):
{{
  "title": "یه عنوان جذاب فارسی برای ویدیو (حداکثر ۸ کلمه)",
  "script": "متن گفتاری ویدیو. صمیمی و خودمونی. ۶۰ تا ۸۰ کلمه. با سلام و یه سوال از مخاطب شروع کن.",
  "youtube_title": "عنوان یوتیوب (با ایموجی، حداکثر ۷۰ کاراکتر)",
  "youtube_description": "توضیحات یوتیوب با هشتگ و CTA به کانال تلگرام @KriptoKlik",
  "youtube_tags": ["تگ۱", "تگ۲", "تگ۳", "تگ۴", "تگ۵"]
}}

فقط JSON برگردون."""

    try:
        response = GEMINI_CLIENT.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip().rstrip("```").strip()
        data = json.loads(text)
        for key in fallback.keys():
            if key not in data:
                data[key] = fallback[key]
        return data
    except Exception as e:
        logger.error("Script generation error: %s: %s", type(e).__name__, str(e)[:300])
        return fallback


async def generate_tts(text: str, output_path: str):
    logger.info("Generating TTS... (Proxy: %s)", PROXY_URL or "disabled")
    communicate = edge_tts.Communicate(text, PERSIAN_VOICE, proxy=PROXY_URL or None)
    await communicate.save(output_path)
    logger.info("TTS saved to %s", output_path)


def fetch_background_image(prompt: str, output_path: str):
    encoded = quote(prompt, safe="")
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=720&height=1280&nologo=true&seed={int(time.time())}"
    r = requests.get(url, timeout=90)
    r.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(r.content)
    logger.info("Background image saved (%d bytes)", len(r.content))


def build_video(image_path: str, audio_path: str, output_path: str):
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-framerate", "2",
        "-i", image_path,
        "-i", audio_path,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "stillimage",
        "-crf", "30",
        "-threads", "1",
        "-c:a", "aac",
        "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2",
        "-r", "24",
        "-movflags", "+faststart",
        output_path,
    ]
    logger.info("Running FFmpeg (low-memory mode)...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        logger.error("FFmpeg stderr (last 800 chars): %s", result.stderr[-800:])
        raise RuntimeError(f"FFmpeg failed: {result.returncode}")
    logger.info("Video built: %s", output_path)


async def create_video(market_summary: str) -> dict:
    os.makedirs(WORKSPACE, exist_ok=True)
    timestamp = int(time.time())
    image_path = f"{WORKSPACE}/bg_{timestamp}.jpg"
    audio_path = f"{WORKSPACE}/voice_{timestamp}.mp3"
    video_path = f"{WORKSPACE}/video_{timestamp}.mp4"

    logger.info("VIDEO: generating script...")
    script_data = generate_video_script(market_summary)

    logger.info("VIDEO: generating TTS...")
    await generate_tts(script_data["script"], audio_path)

    logger.info("VIDEO: fetching background image...")
    img_prompt = (
        f"crypto trading chart, bitcoin, futuristic digital art, "
        f"dark theme, neon lights, professional finance background, "
        f"topic: {script_data['title']}"
    )
    fetch_background_image(img_prompt, image_path)

    logger.info("VIDEO: building MP4...")
    build_video(image_path, audio_path, video_path)

    for f in [image_path, audio_path]:
        try:
            os.remove(f)
        except Exception:
            pass

    return {
        "video_path": video_path,
        "title": script_data["title"],
        "youtube_title": script_data["youtube_title"],
        "youtube_description": script_data["youtube_description"],
        "youtube_tags": script_data["youtube_tags"],
        "script": script_data["script"],
    }


async def build_and_send_post(context: ContextTypes.DEFAULT_TYPE):
    logger.info("POST STEP 1: fetching market data...")
    market_data, summary = fetch_crypto_data()

    logger.info("POST STEP 2: generating text with Gemini...")
    post_text = generate_post_text(summary)

    logger.info("POST STEP 3: waiting 8s...")
    time.sleep(8)

    logger.info("POST STEP 4: fetching chart...")
    chart_url = build_chart_url("bitcoin")
    img_resp = requests.get(chart_url, timeout=60)
    img_resp.raise_for_status()

    photo = BytesIO(img_resp.content)
    photo.name = "chart.png"

    logger.info("POST STEP 5: sending to %s ...", TARGET_CHANNEL)
    try:
        await context.bot.send_photo(
            chat_id=TARGET_CHANNEL,
            photo=photo,
            caption=post_text,
        )
        logger.info("POST done.")
    except TelegramError as e:
        logger.error("send_photo failed: %s", e)
        await context.bot.send_message(chat_id=TARGET_CHANNEL, text=post_text)


async def build_and_send_video_to_admin(context: ContextTypes.DEFAULT_TYPE):
    global LAST_VIDEO_TRANSLATION, LAST_VIDEO_PATH
    logger.info("VIDEO STEP 1: fetching market data...")
    market_data, summary = fetch_crypto_data()

    logger.info("VIDEO STEP 2-5: creating video...")
    video_info = await create_video(summary)
    logger.info("Video created: %s", video_info["video_path"])

    # ذخیره متن و مسیر ویدیو برای استفاده در پست تبلیغاتی
    LAST_VIDEO_TRANSLATION = video_info.get("script", "")
    LAST_VIDEO_PATH = video_info.get("video_path", "")
    logger.info("Saved LAST_VIDEO_TRANSLATION (len: %d) and LAST_VIDEO_PATH", len(LAST_VIDEO_TRANSLATION))

    caption = (
        f"🎬 ویدیوی آماده برای آپلود در یوتیوب\n\n"
        f"📌 عنوان یوتیوب:\n{video_info['youtube_title']}\n\n"
        f"📝 توضیحات یوتیوب:\n{video_info['youtube_description']}\n\n"
        f"🏷 تگ‌ها:\n{', '.join(video_info['youtube_tags'])}\n\n"
        f"📖 اسکریپت:\n{video_info['script']}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔹 ۱. ویدیو رو دانلود کن\n"
        f"🔹 ۲. توی یوتیوب آپلود کن (با همین عنوان/توضیحات/تگ‌ها)\n"
        f"🔹 ۳. لینک یوتیوب رو برام بفرست: /set_youtube_link https://youtu.be/XXXX\n"
        f"🔹 ۴. برای پست در اینستاگرام: /post_ig https://youtu.be/XXXX"
    )

    try:
        with open(video_info["video_path"], "rb") as f:
            await context.bot.send_video(
                chat_id=ADMIN_ID,
                video=f,
                caption=caption[:1024],
                supports_streaming=True,
            )
        logger.info("Video sent to admin DM.")

        if len(caption) > 1024:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=caption[1024:],
            )
    except TelegramError as e:
        logger.error("send_video failed: %s", e)
        raise

    try:
        os.remove(video_info["video_path"])
    except Exception:
        pass


async def set_youtube_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LAST_VIDEO_TRANSLATION
    if not is_admin(update):
        await update.message.reply_text("⛔️ شما اجازه این کار را ندارید.")
        return
    if not context.args:
        await update.message.reply_text(
            "مثال:\n/set_youtube_link https://youtu.be/dQw4w9WgXcQ"
        )
        return

    video_url = context.args[0].strip()
    await update.message.reply_text("⏳ در حال ساخت پست تبلیغاتی...")

    try:
        translation = LAST_VIDEO_TRANSLATION or "یه ویدیوی جدید و جذاب که نباید از دستش بدی!"

        prompt = f"""تو یک کپی‌رایتر حرفه‌ای برای کانال تلگرام کریپتو هستی.
یه ویدیو دوبله شده با این متن:
{translation[:1000]}

حالا یه پست تبلیغاتی جذاب بنویس که کاربر رو ترغیب کنه ویدیو رو ببینه.
شرایط:
- لحن صمیمی و خودمونی
- اشاره به موضوع اصلی ویدیو (نه تحلیل بازار)
- ۵-۸ ایموجی
- آخرش حتماً اینا باشه:
🔗 لینک ویدیو: {video_url}
🔔 ما رو دنبال کنید: @KriptoKlik
#کریپتو #ویدیو

فقط متن پست رو برگردون. کمتر از ۸۰۰ کاراکتر."""

        try:
            resp = GEMINI_CLIENT.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            post_text = resp.text.strip()
        except Exception:
            post_text = (
                f"🎬 ویدیوی جدید ما منتشر شد!\n\n"
                f"{translation[:200]}...\n\n"
                f"🔗 لینک ویدیو: {video_url}\n\n"
                f"🔔 ما رو دنبال کنید: @KriptoKlik\n"
                f"#کریپتو #ویدیو"
            )

        if len(post_text) > CAPTION_LIMIT:
            post_text = post_text[:CAPTION_LIMIT - 3] + "..."

        thumb_url = f"https://img.youtube.com/vi/{video_url.split('v=')[-1].split('/')[-1]}/maxresdefault.jpg"
        if "youtu.be/" in video_url:
            vid_id = video_url.split("youtu.be/")[-1].split("?")[0]
            thumb_url = f"https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg"

        try:
            thumb_resp = requests.get(thumb_url, timeout=30)
            if thumb_resp.status_code == 200 and len(thumb_resp.content) > 1000:
                photo = BytesIO(thumb_resp.content)
                photo.name = "thumb.jpg"
                await context.bot.send_photo(
                    chat_id=TARGET_CHANNEL,
                    photo=photo,
                    caption=post_text,
                )
            else:
                await context.bot.send_message(
                    chat_id=TARGET_CHANNEL,
                    text=post_text,
                )
        except Exception:
            await context.bot.send_message(
                chat_id=TARGET_CHANNEL,
                text=post_text,
            )

        await update.message.reply_text("✅ پست تبلیغاتی در کانال منتشر شد.")
    except Exception as e:
        logger.exception("Error in set_youtube_link")
        await update.message.reply_text(f"❌ خطا: {type(e).__name__}: {str(e)[:200]}")


def schedule_auto_job(app):
    jq = app.job_queue
    for job in jq.get_jobs_by_name(AUTO_JOB_NAME):
        job.schedule_removal()
    jq.run_repeating(auto_post_job, interval=POST_INTERVAL_SECONDS,
                     first=POST_INTERVAL_SECONDS, name=AUTO_JOB_NAME)
    logger.info("Auto-post scheduled every %ds.", POST_INTERVAL_SECONDS)


def schedule_auto_video_job(app):
    jq = app.job_queue
    for job in jq.get_jobs_by_name(AUTO_VIDEO_JOB_NAME):
        job.schedule_removal()
    jq.run_repeating(auto_video_job, interval=AUTO_VIDEO_INTERVAL,
                     first=AUTO_VIDEO_INTERVAL, name=AUTO_VIDEO_JOB_NAME)
    logger.info("Auto-video scheduled every %ds.", AUTO_VIDEO_INTERVAL)


async def auto_post_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Auto-post running...")
    try:
        await build_and_send_post(context)
        logger.info("Auto-post done.")
    except Exception:
        logger.exception("Auto-post failed")


async def auto_video_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Auto-video running...")
    try:
        await build_and_send_video_to_admin(context)
        logger.info("Auto-video done.")
    except Exception:
        logger.exception("Auto-video failed")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 👋\n\n"
        "🎬 دوبله ویدیو:\n"
        "/dub <لینک> — دوبله ویدیوی یوتیوب\n"
        "/set_webhook <آدرس> — تنظیم آدرس Colab\n"
        "/check_colab — بررسی وضعیت Colab\n\n"
        "📸 اینستاگرام:\n"
        "/insta_login — لاگین به اینستاگرام\n"
        "/check_instagram — بررسی وضعیت اینستاگرام\n"
        "/post_ig <لینک> — پست در اینستاگرام\n\n"
        "📝 پست متنی:\n"
        "/generate_post — پست متنی فوری در کانال\n"
        "/generate_video — ساخت ویدیو + ارسال به چت خصوصی\n"
        "/set_youtube_link <url> — پست تبلیغاتی ویدیو در کانال\n"
        "/set_channel @name — تغییر کانال\n"
        "/show_channel — نمایش کانال\n"
        "/debug — اطلاعات عیب‌یابی\n"
        "/auto_on | /auto_off | /auto_status"
    )


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️")
        return
    jobs = context.job_queue.get_jobs_by_name(AUTO_JOB_NAME)
    vjobs = context.job_queue.get_jobs_by_name(AUTO_VIDEO_JOB_NAME)
    next_run = jobs[0].next_t.strftime("%Y-%m-%d %H:%M UTC") if jobs and jobs[0].next_t else "—"
    next_vid = vjobs[0].next_t.strftime("%Y-%m-%d %H:%M UTC") if vjobs and vjobs[0].next_t else "—"

    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        ffmpeg_ok = r.returncode == 0
    except Exception:
        ffmpeg_ok = False

    msg = (
        f"🔧 اطلاعات:\n\n"
        f"ADMIN_ID: {ADMIN_ID}\n"
        f"TARGET_CHANNEL: {repr(TARGET_CHANNEL)}\n"
        f"GEMINI: {'✅' if GEMINI_CLIENT else '❌'}\n"
        f"FFmpeg: {'✅' if ffmpeg_ok else '❌'}\n"
        f"Voice: {PERSIAN_VOICE}\n"
        f"Proxy: {PROXY_URL or '❌'}\n"
        f"POST_INTERVAL: {POST_INTERVAL_SECONDS}s\n"
        f"VIDEO_INTERVAL: {AUTO_VIDEO_INTERVAL}s ({AUTO_VIDEO_INTERVAL // 3600}h)\n"
        f"AUTO_POST: {'🟢' if jobs else '🔴'} ({next_run})\n"
        f"AUTO_VIDEO: {'🟢' if vjobs else '🔴'} ({next_vid})"
    )
    await update.message.reply_text(msg)


async def show_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"کانال فعلی: {TARGET_CHANNEL}")


async def set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHANNEL
    if not is_admin(update):
        await update.message.reply_text("⛔️")
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
        await update.message.reply_text("⛔️")
        return
    await update.message.reply_text("⏳ در حال ساخت پست هوشمند...")
    try:
        await build_and_send_post(context)
        await update.message.reply_text("✅ پست منتشر شد.")
    except Exception as e:
        logger.exception("Error generate_post")
        await update.message.reply_text(f"❌ {type(e).__name__}: {str(e)[:200]}")


async def generate_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️")
        return
    await update.message.reply_text(
        "⏳ در حال ساخت ویدیو...\n"
        "این کار ۲ تا ۵ دقیقه طول می‌کشه. بعدش ویدیو رو توی چت خصوصی برات می‌فرستم."
    )
    try:
        await build_and_send_video_to_admin(context)
        await update.message.reply_text(
            "✅ ویدیو ساخته و به چت خصوصی ارسال شد.\n"
            "حالا دانلودش کن، توی یوتیوب آپلود کن، و لینکش رو با /set_youtube_link برام بفرست.\n"
            "برای پست در اینستاگرام هم از /post_ig استفاده کن."
        )
    except Exception as e:
        logger.exception("Error generate_video")
        await update.message.reply_text(f"❌ {type(e).__name__}: {str(e)[:200]}")


async def auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️")
        return
    schedule_auto_job(context.application)
    await update.message.reply_text(
        f"✅ پست خودکار روشن شد. هر {POST_INTERVAL_SECONDS // 3600} ساعت."
    )


async def auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️")
        return
    for job in context.job_queue.get_jobs_by_name(AUTO_JOB_NAME):
        job.schedule_removal()
    await update.message.reply_text("🛑 پست خودکار خاموش شد.")


async def auto_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = context.job_queue.get_jobs_by_name(AUTO_JOB_NAME)
    vjobs = context.job_queue.get_jobs_by_name(AUTO_VIDEO_JOB_NAME)
    txt = ""
    if jobs and jobs[0].next_t:
        txt += f"🟢 پست فعال — بعدی: {jobs[0].next_t.strftime('%Y-%m-%d %H:%M UTC')}\n"
    else:
        txt += "🔴 پست خاموش\n"
    if vjobs and vjobs[0].next_t:
        txt += f"🎬 ویدیو فعال — بعدی: {vjobs[0].next_t.strftime('%Y-%m-%d %H:%M UTC')}"
    else:
        txt += "🎬 ویدیو خاموش"
    await update.message.reply_text(txt)


async def auto_video_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️")
        return
    schedule_auto_video_job(context.application)
    await update.message.reply_text(
        f"✅ ساخت خودکار ویدیو روشن شد. هر {AUTO_VIDEO_INTERVAL // 3600} ساعت."
    )


async def auto_video_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️")
        return
    for job in context.job_queue.get_jobs_by_name(AUTO_VIDEO_JOB_NAME):
        job.schedule_removal()
    await update.message.reply_text("🛑 ساخت خودکار ویدیو خاموش شد.")


async def post_init(app):
    if AUTO_POST_ENABLED:
        schedule_auto_job(app)
    if AUTO_VIDEO_ENABLED:
        schedule_auto_video_job(app)


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN تنظیم نشده.")

    from multiprocessing import Process
    web_proc = Process(target=run_web_server, daemon=True)
    web_proc.start()
    logger.info("Web server process started (PID: %s)", web_proc.pid)

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
    app.add_handler(CommandHandler("generate_video", generate_video))
    app.add_handler(CommandHandler("set_youtube_link", set_youtube_link))
    app.add_handler(CommandHandler("auto_on", auto_on))
    app.add_handler(CommandHandler("auto_off", auto_off))
    app.add_handler(CommandHandler("auto_status", auto_status))
    app.add_handler(CommandHandler("auto_video_on", auto_video_on))
    app.add_handler(CommandHandler("auto_video_off", auto_video_off))

    from dubbing import register_dubbing_handlers
    register_dubbing_handlers(app)

    from instagram import register_instagram_handlers
    register_instagram_handlers(app)

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
