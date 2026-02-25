import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

def text_to_mp3(text: str, out_path: str) -> str:
    load_dotenv()
    client = OpenAI()

    model = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
    voice = os.getenv("TTS_VOICE", "alloy")

    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    # Generate speech
    response = client.audio.speech.create(
        model=model,
        voice=voice,
        input=text,
    )

    # Save MP3 manually
    with open(out_file, "wb") as f:
        f.write(response.content)

    return str(out_file)
