import imaplib
import email
import re
from instagrapi import Client
from instagrapi.mixins.challenge import ChallengeChoice

# اطلاعات ایمیل خود را اینجا وارد کن (بهتر است از متغیرهای محیطی Railway استفاده کنی)
EMAIL_USER = "your_email@gmail.com"
EMAIL_PASS = "your_app_password"  # برای جیمیل باید "رمز برنامه" بسازی

def get_code_from_email(username):
    """به ایمیل وصل می‌شود و کد تأیید اینستاگرام را پیدا می‌کند."""
    try:
        # برای جیمیل از imap.gmail.com استفاده کن
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")

        # جستجوی ایمیل‌های خوانده نشده
        result, data = mail.search(None, "(UNSEEN)")
        if result != "OK":
            return False

        ids = data[0].split()
        for num in reversed(ids):
            # دریافت محتوای ایمیل
            result, data = mail.fetch(num, "(RFC822)")
            if result != "OK":
                continue
            
            msg = email.message_from_bytes(data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode()
                        break
            else:
                body = msg.get_payload(decode=True).decode()

            # جستجوی کد ۶ رقمی در متن ایمیل
            # الگوی رایج: "Your Instagram code is: 123456"
            match = re.search(r'(\d{6})', body)
            if match:
                # ایمیل را خوانده شده علامت بزن
                mail.store(num, '+FLAGS', '\\Seen')
                return match.group(1)
        
        return False
    except Exception as e:
        print(f"Error reading email: {e}")
        return False

def challenge_code_handler(username, choice):
    """این تابع توسط instagrapi صدا زده می‌شود."""
    if choice == ChallengeChoice.EMAIL:
        return get_code_from_email(username)
    # اگر از طریق SMS خواست (که معمولاً در ایران کار نمی‌کند)
    elif choice == ChallengeChoice.SMS:
        return False  # نمی‌توانیم SMS را خودکار بخوانیم
    return False

# --- استفاده در کد اصلی ---
# cl = Client()
# cl.challenge_code_handler = challenge_code_handler
# cl.login(USERNAME, PASSWORD)