import logging
import re
from typing import Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import LOG_LEVEL, TELEGRAM_BOT_TOKEN, MIN_PLAYERS, REQUIRED_COMMON_WORDS
from dictionary_parser import parse_dictionary
from game import Game, Phase
import player_stats

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
class _HttpxDebugFilter(logging.Filter):
    """Hide httpx HTTP Request log messages unless log level is DEBUG."""
    def filter(self, record: logging.LogRecord) -> bool:
        if "HTTP Request" in record.getMessage():
            return record.levelno > logging.DEBUG or logging.getLogger().isEnabledFor(logging.DEBUG)
        return True

logging.getLogger("httpx").addFilter(_HttpxDebugFilter())
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


def _menu_keyboard_for(user_id: int) -> InlineKeyboardMarkup:
    """Build a context-aware menu keyboard showing only valid actions."""
    game = _find_game_for_user(user_id)
    buttons = []

    if not game or game.phase == Phase.FINISHED:
        # Not in any active game
        buttons.append([InlineKeyboardButton("🎲 Новая игра", callback_data="menu:newgame")])
        buttons.append([InlineKeyboardButton("📋 Открытые комнаты", callback_data="menu:games")])
    elif game.phase == Phase.LOBBY:
        if game.creator_id == user_id:
            # Creator in lobby
            if len(game.players) >= MIN_PLAYERS:
                buttons.append([InlineKeyboardButton("▶️ Начать игру", callback_data="menu:startgame")])
            buttons.append([InlineKeyboardButton("📋 Открытые комнаты", callback_data="menu:games")])
            buttons.append([InlineKeyboardButton("❌ Отменить комнату", callback_data="menu:cancel")])
        else:
            # Non-creator in lobby
            buttons.append([InlineKeyboardButton("❌ Покинуть комнату", callback_data="menu:cancel")])
    else:
        # Active game (SELECTING / WRITING / GUESSING)
        buttons.append([InlineKeyboardButton("❌ Выйти из игры", callback_data="menu:cancel")])

    return InlineKeyboardMarkup(buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    dictionary = context.bot_data["dictionary"]
    user_id = update.effective_user.id
    await update.message.reply_text(
        f"Привет! Я — бот-философ.\n"
        f"В моём словаре {len(dictionary)} статей.\n\n"
        f"Просто напиши название термина, и я найду его определение.\n\n"
        f"🎲 Игра «Угадай определение» — выберите действие:",
        reply_markup=_menu_keyboard_for(user_id),
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

    # ── If this is a private message and the user is in a WRITING phase, handle it ──
    if update.effective_chat.type == "private":
        handled = await _try_handle_writing(update, context)
        if handled:
            return

    dictionary = context.bot_data["dictionary"]
    result = _lookup(text, dictionary)

    if result is None:
        await update.message.reply_text("Статья не найдена. Попробуй другой запрос.")
        return

    title, article = result
    header = f"📖 *{title}*\n\n"
    max_body = MAX_MESSAGE_LEN - len(header)

    # Split article into sentences and keep as many whole ones as fit.
    sentences = re.split(r'(?<=[.!?…])\s+', article)
    body = ""
    for sentence in sentences:
        candidate = body + (" " if body else "") + sentence
        if len(candidate) > max_body:
            break
        body = candidate

    if not body:
        body = article[:max_body]

    await update.message.reply_text(header + body)


# ── Game state ────────────────────────────────────────────────
# room_id -> Game
rooms: Dict[int, Game] = {}


def _name(user) -> str:
    """Build a readable display name for a Telegram user."""
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    return user.first_name or user.username or str(user.id)


async def _broadcast(context: ContextTypes.DEFAULT_TYPE, game: Game, text: str, **kwargs) -> None:
    """Send a message to every player in the room via DM."""
    for player in game.players.values():
        try:
            await context.bot.send_message(player.user_id, text, **kwargs)
        except Exception as e:
            logger.error("Cannot DM user %s: %s", player.user_id, e)


# ── Menu callback ─────────────────────────────────────────────

async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle main-menu inline button presses."""
    query = update.callback_query
    await query.answer()
    user = query.from_user
    _, action = query.data.split(":", 1)

    if action == "main":
        dictionary = context.bot_data["dictionary"]
        await query.edit_message_text(
            f"Привет! Я — бот-философ.\n"
            f"В моём словаре {len(dictionary)} статей.\n\n"
            f"Просто напиши название термина, и я найду его определение.\n\n"
            f"🎲 Игра «Угадай определение» — выберите действие:",
            reply_markup=_menu_keyboard_for(user.id),
        )
    elif action == "newgame":
        text = await _do_newgame(user, context)
        await query.edit_message_text(text, reply_markup=_menu_keyboard_for(user.id))
    elif action == "games":
        text, kb = _build_games_keyboard(user.id)
        await query.edit_message_text(text, reply_markup=kb)
    elif action == "startgame":
        err = await _do_startgame(user.id, context)
        if err:
            await query.edit_message_text(err, reply_markup=_menu_keyboard_for(user.id))
        else:
            await query.edit_message_text(
                "🎲 Игра запущена! Проверьте личные сообщения.",
                reply_markup=_menu_keyboard_for(user.id),
            )
    elif action == "cancel":
        text = await _do_cancel(user.id, user, context)
        await query.edit_message_text(text, reply_markup=_menu_keyboard_for(user.id))


# ── /newgame — create a room ─────────────────────────────────

async def _do_newgame(user, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Core newgame logic. Returns reply text."""
    user_id = user.id
    existing = _find_game_for_user(user_id)
    if existing and existing.phase != Phase.FINISHED:
        return (
            f"Вы уже в комнате «{existing.room_name}» (#{existing.room_id}). "
            f"Сначала выйдите."
        )
    dictionary = context.bot_data["dictionary"]
    name = _name(user)
    game = Game(room_name=f"Комната {name}", creator_id=user_id, dictionary=dictionary)
    rooms[game.room_id] = game
    game.add_player(user_id, name)
    player_stats.record_join(user_id, name)
    return (
        f"🎲 Комната «{game.room_name}» (#{game.room_id}) создана!\n"
        f"Вы автоматически присоединены.\n\n"
        f"Другие игроки могут присоединиться через меню «Открытые комнаты».\n"
        f"Когда все готовы — нажмите «Начать игру»."
    )


async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await _do_newgame(update.effective_user, context)
    await update.message.reply_text(text, reply_markup=_menu_keyboard_for(update.effective_user.id))


# ── /games — list open rooms ─────────────────────────────────

def _build_games_keyboard(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build text + keyboard for open rooms list."""
    lobbies = [g for g in rooms.values() if g.phase == Phase.LOBBY]
    if not lobbies:
        return "Нет открытых комнат.", _menu_keyboard_for(user_id)
    buttons = []
    for g in lobbies:
        label = f"#{g.room_id} {g.room_name} ({len(g.players)} игр.)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"join:{g.room_id}")])
    buttons.append([InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main")])
    return "🎲 Открытые комнаты (нажмите чтобы войти):", InlineKeyboardMarkup(buttons)


async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = _build_games_keyboard(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=kb)


# ── /join callback — join a room via button ──────────────────

async def cb_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_id = user.id

    _, room_id_str = query.data.split(":", 1)
    room_id = int(room_id_str)
    game = rooms.get(room_id)

    if not game or game.phase != Phase.LOBBY:
        await query.edit_message_text("Эта комната уже недоступна.", reply_markup=_menu_keyboard_for(user_id))
        return

    existing = _find_game_for_user(user_id)
    if existing and existing.room_id != room_id and existing.phase != Phase.FINISHED:
        await query.edit_message_text(
            f"Вы уже в комнате «{existing.room_name}» (#{existing.room_id}). "
            f"Сначала выйдите.",
            reply_markup=_menu_keyboard_for(user_id),
        )
        return

    name = _name(user)
    ok, msg = game.add_player(user_id, name)
    if ok:
        player_stats.record_join(user_id, name)
    await query.edit_message_text(msg, reply_markup=_menu_keyboard_for(user_id))
    if ok:
        # Notify other players in the room (with updated menu)
        for p in game.players.values():
            if p.user_id != user_id:
                try:
                    await context.bot.send_message(
                        p.user_id,
                        f"👋 {name} присоединился к комнате «{game.room_name}»! "
                        f"Игроков: {len(game.players)}",
                        reply_markup=_menu_keyboard_for(p.user_id),
                    )
                except Exception:
                    pass


# ── /startgame — begin selection phase ───────────────────────

async def _do_startgame(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Core startgame logic. Returns error text or None on success."""
    game = _find_game_for_user(user_id)
    if not game or game.phase != Phase.LOBBY:
        return "Вы не в комнате ожидания."
    if game.creator_id != user_id:
        return "Только создатель комнаты может начать игру."
    ok, msg = game.can_start()
    if not ok:
        return msg
    words = game.start_selection()
    await _send_selection_to_all(context, game, words)
    return None


async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = await _do_startgame(update.effective_user.id, context)
    if err:
        await update.message.reply_text(err, reply_markup=_menu_keyboard_for(update.effective_user.id))


# ── /cancel — leave / cancel room ────────────────────────────

async def _do_cancel(user_id: int, user, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Core cancel logic. Returns reply text."""
    game = _find_game_for_user(user_id)
    if not game:
        return "Вы не в игре."
    room_name = game.room_name
    room_id = game.room_id
    if game.creator_id == user_id:
        del rooms[room_id]
        for p in game.players.values():
            if p.user_id != user_id:
                try:
                    await context.bot.send_message(
                        p.user_id,
                        f"❌ Комната «{room_name}» отменена создателем.",
                        reply_markup=_menu_keyboard_for(p.user_id),
                    )
                except Exception:
                    pass
        return f"Комната «{room_name}» отменена."
    else:
        del game.players[user_id]
        await _broadcast(
            context, game,
            f"👤 {_name(user)} покинул(а) комнату. Игроков: {len(game.players)}"
        )
        return f"Вы покинули комнату «{room_name}»."


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await _do_cancel(update.effective_user.id, update.effective_user, context)
    await update.message.reply_text(text, reply_markup=_menu_keyboard_for(update.effective_user.id))


# ── Selection phase helpers ──────────────────────────────────

def _build_selection_keyboard(game: Game, user_id: int) -> InlineKeyboardMarkup:
    """Build inline keyboard with word buttons + confirm."""
    player = game.players[user_id]
    buttons = []
    for idx, word in enumerate(game.current_word_set):
        mark = "✅ " if word in player.selected_words else ""
        buttons.append([InlineKeyboardButton(
            f"{mark}{word}", callback_data=f"sel:{idx}"
        )])
    buttons.append([InlineKeyboardButton("✔️ Подтвердить выбор", callback_data="sel:OK")])
    return InlineKeyboardMarkup(buttons)


async def _send_selection_to_all(context: ContextTypes.DEFAULT_TYPE, game: Game, words: list[str]) -> None:
    """Send word selection keyboard to every player via DM."""
    for player in game.players.values():
        kb = _build_selection_keyboard(game, player.user_id)
        try:
            await context.bot.send_message(
                player.user_id,
                f"📋 Комната «{game.room_name}»\n"
                f"Отметьте слова, значения которых вы НЕ знаете, и нажмите «Подтвердить»:",
                reply_markup=kb,
            )
        except Exception as e:
            logger.error("Cannot DM user %s: %s", player.user_id, e)


async def cb_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button presses during the SELECTING phase."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # Find which game this player is in
    game = _find_game_for_user(user_id)
    if not game or game.phase != Phase.SELECTING:
        return
    if user_id not in game.players:
        return

    data = query.data  # "sel:<index>" or "sel:OK"
    _, value = data.split(":", 1)

    if value == "OK":
        game.confirm_selection(user_id)
        await query.edit_message_text("✅ Выбор подтверждён! Ожидайте остальных игроков.")
        player_name = game.players[user_id].display_name
        for p in game.players.values():
            if p.user_id != user_id:
                try:
                    await context.bot.send_message(
                        p.user_id, f"{player_name} подтвердил(а) выбор."
                    )
                except Exception:
                    pass
        if game.all_selected():
            await _process_selection_results(context, game)
    else:
        idx = int(value)
        word = game.current_word_set[idx]
        game.toggle_word(user_id, word)
        kb = _build_selection_keyboard(game, user_id)
        await query.edit_message_reply_markup(reply_markup=kb)


async def _process_selection_results(context: ContextTypes.DEFAULT_TYPE, game: Game) -> None:
    """Check intersection; either start writing or ask for a new round."""
    common = game.compute_intersection()
    if len(common) >= REQUIRED_COMMON_WORDS:
        game.start_writing(common)
        await _broadcast(
            context, game,
            f"🎯 Отлично! Набрано общих незнакомых слов: {len(common)}. "
            f"Используем {REQUIRED_COMMON_WORDS}.\n\n"
            f"Фаза сочинения определений!"
        )
        for player in game.players.values():
            word = game.current_writing_word(player.user_id)
            if word:
                await _send_writing_prompt(context, player.user_id, word)
    else:
        words = game.new_word_set()
        await _broadcast(
            context, game,
            f"Накоплено общих незнакомых слов: {len(common)} из {REQUIRED_COMMON_WORDS}.\n"
            f"Нужно ещё — новый набор!"
        )
        await _send_selection_to_all(context, game, words)


# ── Writing phase ─────────────────────────────────────────────

async def _send_writing_prompt(context: ContextTypes.DEFAULT_TYPE, user_id: int, word: str) -> None:
    """Send a prompt to write a fake definition for a word."""
    game = _find_game_for_user(user_id)
    if not game:
        return
    player = game.players[user_id]
    idx = player.writing_index + 1
    total = len(game.game_words)
    await context.bot.send_message(
        user_id,
        f"✍️ Слово {idx}/{total}: *{word}*\n\n"
        f"Придумайте определение, похожее на словарное, и отправьте его сообщением.",
        parse_mode="Markdown",
    )


async def _try_handle_writing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Try to handle a private message as a writing-phase input. Returns True if handled."""
    user_id = update.effective_user.id
    game = _find_game_for_user(user_id)
    if not game or game.phase != Phase.WRITING:
        return False
    if user_id not in game.players:
        return False

    player = game.players[user_id]
    current_word = game.current_writing_word(user_id)
    if current_word is None:
        return False

    definition = update.message.text.strip()
    if not definition:
        return False

    next_word = game.submit_definition(user_id, definition)

    # Notify other players in the room
    for p in game.players.values():
        if p.user_id != user_id:
            try:
                await context.bot.send_message(
                    p.user_id,
                    f"📝 {player.display_name} написал(а) определение для «{current_word}»."
                )
            except Exception:
                pass

    if next_word:
        await _send_writing_prompt(context, user_id, next_word)
    else:
        await context.bot.send_message(
            user_id,
            "✅ Вы написали все определения! Ожидайте остальных игроков."
        )

    if game.all_finished_writing():
        await _start_guessing_phase(context, game)

    return True


async def _start_guessing_phase(context: ContextTypes.DEFAULT_TYPE, game: Game) -> None:
    """Transition to guessing and send first word to everyone."""
    game.start_guessing()
    await _broadcast(
        context, game,
        "🔍 Все определения написаны! Начинается фаза угадывания."
    )
    for player in game.players.values():
        word = game.current_guessing_word(player.user_id)
        if word:
            await _send_guessing_prompt(context, game, player.user_id, word)


# ── Guessing phase ────────────────────────────────────────────

async def _send_guessing_prompt(
    context: ContextTypes.DEFAULT_TYPE, game: Game, user_id: int, word: str
) -> None:
    player = game.players[user_id]
    idx = player.guessing_index + 1
    total = len(game.game_words)
    defs = game.get_definitions_for_player(user_id, word)

    text = f"🔮 Слово {idx}/{total}: «{word}»\n\nКакое определение настоящее?\n"
    buttons = []
    for i, (def_key, def_text) in enumerate(defs, 1):
        # Truncate long definition text in the message body
        display = def_text[:500] + "…" if len(def_text) > 500 else def_text
        text += f"\n{i}. {display}\n"
        buttons.append([InlineKeyboardButton(
            f"Вариант {i}", callback_data=f"guess:{def_key}"
        )])

    kb = InlineKeyboardMarkup(buttons)
    # Send without parse_mode to avoid issues with special chars in definitions
    await context.bot.send_message(user_id, text, reply_markup=kb)


async def cb_guess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle guess button presses."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    game = _find_game_for_user(user_id)
    if not game or game.phase != Phase.GUESSING:
        return
    if user_id not in game.players:
        return

    _, def_key = query.data.split(":", 1)
    player = game.players[user_id]
    current_word = game.current_guessing_word(user_id)

    if current_word is None:
        return

    next_word = game.submit_guess(user_id, def_key)

    await query.edit_message_text(
        f"Ваш ответ для «{current_word}» принят! (Узнаете результат в конце)"
    )

    for p in game.players.values():
        if p.user_id != user_id:
            try:
                await context.bot.send_message(
                    p.user_id,
                    f"🎯 {player.display_name} ответил(а) на «{current_word}»."
                )
            except Exception:
                pass

    if next_word:
        await _send_guessing_prompt(context, game, user_id, next_word)
    else:
        await context.bot.send_message(
            user_id,
            "✅ Вы ответили на все слова! Ожидайте остальных игроков."
        )

    if game.all_finished_guessing():
        await _show_results(context, game)


# ── Results ───────────────────────────────────────────────────

async def _show_results(context: ContextTypes.DEFAULT_TYPE, game: Game) -> None:
    """Compute scores and broadcast results."""
    results = game.compute_scores()
    word_breakdown = game.get_word_results()

    # Per-word breakdown (no parse_mode — definitions may contain special chars)
    for word, real_def, defs_info in word_breakdown:
        text = f"📖 {word}\n\n"
        for label, def_text, choosers in defs_info:
            who = ", ".join(choosers) if choosers else "никто"
            short = def_text[:400] + "…" if len(def_text) > 400 else def_text
            text += f"{label}:\n{short}\nВыбрали: {who}\n\n"
        # Truncate if too long
        if len(text) > MAX_MESSAGE_LEN:
            text = text[:MAX_MESSAGE_LEN - 20] + "\n…(обрезано)"
        await _broadcast(context, game, text)

    # Scoreboard with shared places
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    scoreboard = "🏆 Итоги игры:\n\n"
    for place, uid, name, score, details in results:
        medal = medals.get(place, f"{place}.")
        scoreboard += f"{medal} {name} — {score} очков\n"
        for d in details:
            scoreboard += f"{d}\n"
        scoreboard += "\n"

    scoreboard += "\nНовая игра — нажмите «Новая игра» в меню."
    for player in game.players.values():
        try:
            await context.bot.send_message(
                player.user_id, scoreboard,
                reply_markup=_menu_keyboard_for(player.user_id),
            )
        except Exception as e:
            logger.error("Cannot DM user %s: %s", player.user_id, e)

    # ── Log player stats ──
    all_names = {p.user_id: p.display_name for p in game.players.values()}
    for place, uid, pname, score, _ in results:
        co_players = [n for u, n in all_names.items() if u != uid]
        player_stats.record_game_finished(
            user_id=uid,
            display_name=pname,
            is_winner=(place == 1),
            co_player_names=co_players,
        )

    # Cleanup
    del rooms[game.room_id]


# ── Helpers ───────────────────────────────────────────────────

def _find_game_for_user(user_id: int) -> Game | None:
    """Find a game room where this user_id is a player."""
    for game in rooms.values():
        if user_id in game.players:
            return game
    return None


def main() -> None:
    dictionary = parse_dictionary()
    logger.info("Loaded %d articles", len(dictionary))

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.bot_data["dictionary"] = dictionary

    # ── Dictionary lookup handlers ──
    app.add_handler(CommandHandler("start", start))

    # ── Game command handlers ──
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("games", cmd_games))
    app.add_handler(CommandHandler("startgame", cmd_startgame))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # ── Game callback handlers ──
    app.add_handler(CallbackQueryHandler(cb_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(cb_join, pattern=r"^join:"))
    app.add_handler(CallbackQueryHandler(cb_selection, pattern=r"^sel:"))
    app.add_handler(CallbackQueryHandler(cb_guess, pattern=r"^guess:"))

    # ── Text handler (dictionary lookup + writing phase in DM) ──
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Log everything in a separate group so it doesn't block other handlers.
    app.add_handler(MessageHandler(filters.ALL, log_message), group=1)

    logger.info("Bot started, polling for updates…")
    app.run_polling()


if __name__ == "__main__":
    main()
