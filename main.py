import os
import logging
import io
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import google.generativeai as genai
from huggingface_hub import InferenceClient

# تنظیمات لاگ‌گیری
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# خواندن کلیدها از متغیرهای محیطی Railway
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
HUGGINGFACE_KEY = os.getenv("HUGGINGFACE_API_KEY")

# تنظیمات Gemini
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-3.8-flash')

# راه‌اندازی کلاینت استاندارد Hugging Face
hf_client = InferenceClient(token=HUGGINGFACE_KEY)

# متغیر برای ذخیره آدرس کانال
TARGET_CHANNEL = "@your_channel_username"

async def set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHANNEL
    if context.args:
        TARGET_CHANNEL = context.args[0]
        await update.message.reply_text(f"کانال با موفقیت به {TARGET_CHANNEL} تغییر یافت.")
    else:
        await update.message.reply_text("لطفاً آیدی کانال را وارد کنید. مثال:\n/set_channel @mychannel")

async def generate_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("در حال تولید متن و تصویر با هوش مصنوعی... لطفاً صبور باشید.")
    
    try:
        # ۱. تولید متن جذاب با Gemini
        prompt_text = "یک پست کوتاه، جذاب و آموزنده درباره تکنولوژی یا موفقیت به زبان فارسی بنویس که مناسب کانال تلگرام باشد."
        response = gemini_model.generate_content(prompt_text)
        post_text = response.text

        # ۲. تولید تصویر با کتابخانه رسمی Hugging Face
        image_prompt = "A modern futuristic workspace, high quality, digital art"
        image = hf_client.text_to_image(
            image_prompt,
            model="stabilityai/stable-diffusion-xl-base-1.0"
        )
        
        # تبدیل تصویر PIL به بایت برای ارسال در تلگرام
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG')
        image_bytes = img_byte_arr.getvalue()
        
        # ۳. ارسال عکس و متن به کانال تلگرام
        await context.bot.send_photo(
            chat_id=TARGET_CHANNEL,
            photo=image_bytes,
            caption=post_text
        )
        await update.message.reply_text("پست با موفقیت در کانال منتشر شد!")

    except Exception as e:
        await update.message.reply_text(f"خطایی رخ داد: {str(e)}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("set_channel", set_channel))
    app.add_handler(CommandHandler("generate_post", generate_post))

    print("Bot is running...")
    app.run_polling()

if __name__ == '__main__':
    main()
