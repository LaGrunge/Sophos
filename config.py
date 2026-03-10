import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill in your token.")

# ── Game settings ──────────────────────────────────────────────
MAX_PLAYERS = int(os.getenv("MAX_PLAYERS", "10"))
WORDS_PER_ROUND = int(os.getenv("WORDS_PER_ROUND", "10"))
REQUIRED_COMMON_WORDS = int(os.getenv("REQUIRED_COMMON_WORDS", "5"))
MIN_PLAYERS = int(os.getenv("MIN_PLAYERS", "2"))
