from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI()

resp = client.responses.create(
    model="gpt-4o-mini",
    input="Reply with exactly: API WORKS"
)

print(resp.output_text)
