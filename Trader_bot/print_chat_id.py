import os
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN")
if not token:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")

url = f"https://api.telegram.org/bot{token}/getUpdates"
data = requests.get(url, timeout=30).json()

results = data.get("result", [])
if not results:
    print("No messages found. In Telegram, open your bot and send 'hi' then run again.")
    raise SystemExit()

# Grab the most recent chat id
last = results[-1]
chat_id = last["message"]["chat"]["id"]
print(chat_id)