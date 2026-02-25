from dotenv import load_dotenv
import os

load_dotenv()
token = os.getenv("TELEGRAM_BOT_TOKEN", "")
chat = os.getenv("TELEGRAM_CHAT_ID", "")

print("TOKEN starts:", token[:10])
print("TOKEN contains colon:", ":" in token)
print("TOKEN length:", len(token))
print("CHAT_ID:", chat)
