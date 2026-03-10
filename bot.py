import logging

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

from config import TELEGRAM_BOT_TOKEN
from dictionary_parser import parse_dictionary

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


MAX_MESSAGE_LEN = 4096


async def log_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    logger.info(
        "chat_id=%s chat_type=%s user_id=%s username=%s text=%r",
        chat.id if chat else None,
        chat.type if chat else None,
        user.id if user else None,
        user.username if user else None,
        message.text if message else None,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    dictionary = context.bot_data["dictionary"]
    await update.message.reply_text(
        f"Привет! Я — бот-философ.\n"
        f"В моём словаре {len(dictionary)} статей.\n\n"
        f"Просто напиши название термина, и я найду его определение."
    )


def _lookup(query: str, dictionary: dict[str, str]) -> tuple[str, str] | None:
    """Find an article by query. Returns (title, text) or None."""
    q = query.strip().upper()
    # 1) Exact match (case-insensitive).
    for title, text in dictionary.items():
        if title.upper() == q:
            return title, text
    # 2) Substring match — title starts with query.
    for title, text in dictionary.items():
        if title.upper().startswith(q):
            return title, text
    # 3) Substring match — query is contained in title.
    for title, text in dictionary.items():
        if q in title.upper():
            return title, text
    return None


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    if not text:
        return

    dictionary = context.bot_data["dictionary"]
    result = _lookup(text, dictionary)

    if result is None:
        await update.message.reply_text("Статья не найдена. Попробуй другой запрос.")
        return

    title, article = result
    reply = f"📖 *{title}*\n\n{article}"

    # Telegram message limit is 4096 chars; split if needed.
    if len(reply) <= MAX_MESSAGE_LEN:
        await update.message.reply_text(reply)
    else:
        # Send in chunks.
        for i in range(0, len(reply), MAX_MESSAGE_LEN):
            await update.message.reply_text(reply[i : i + MAX_MESSAGE_LEN])


def main() -> None:
    dictionary = parse_dictionary()
    logger.info("Loaded %d articles", len(dictionary))

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.bot_data["dictionary"] = dictionary

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # Log everything in a separate group so it doesn't block other handlers.
    app.add_handler(MessageHandler(filters.ALL, log_message), group=1)

    logger.info("Bot started, polling for updates…")
    app.run_polling()


if __name__ == "__main__":
    main()
