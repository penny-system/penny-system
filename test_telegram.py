from dotenv import load_dotenv
import os
from app.alerts import send_telegram_text

load_dotenv()

send_telegram_text("🚀 Telegram test successful. Next step: audio brief.")
print("Telegram message sent.")
