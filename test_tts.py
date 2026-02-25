from dotenv import load_dotenv
from app.tts import text_to_mp3
from app.alerts import send_telegram_text, send_telegram_file

load_dotenv()

brief = (
    "Good morning. This is your three minute market brief test. "
    "If you can hear this, your audio pipeline is working. "
    "Next we will generate the daily top ten deck and the top five audio summary."
)

send_telegram_text("🎧 Generating a test MP3 now...")
mp3_path = text_to_mp3(brief, out_path="data/test_brief.mp3")
send_telegram_file(mp3_path, caption="✅ Test audio brief (MP3)")
print("Done. Check Telegram for the MP3.")
