import json
import logging
import os
import threading
import time
import zoneinfo
from datetime import datetime, time as dtime
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from word_provider import WordProvider

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("BYNARA_API_KEY")
BASE_URL = os.getenv("BYNARA_BASE_URL", "https://router.bynara.id/v1")
MODEL = os.getenv("BYNARA_MODEL", "mistral-medium-3-5")

# Allowlist — comma-separated chat IDs, empty = open to all
# Group/supergroup chat IDs are negative, so we can't use str.isdigit() here.
def _parse_allowed_ids(raw: str) -> set[int]:
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning("Ignoring invalid ALLOWED_CHAT_IDS entry: %r", part)
    return ids


ALLOWED_IDS_RAW = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = _parse_allowed_ids(ALLOWED_IDS_RAW)

# STATE_DIR should point at a mounted persistent disk in production (see
# render.yaml) — Render's default filesystem is ephemeral and is wiped on
# every deploy/restart, which would otherwise silently drop all subscribers
# and reset the used-words history.
STATE_DIR = Path(os.getenv("STATE_DIR", str(Path(__file__).parent)))
STATE_FILE = STATE_DIR / "state.json"
STATE_LOCK = threading.Lock()
TZ = zoneinfo.ZoneInfo("Africa/Lagos")

# Cap on how many used words we remember, to keep the prompt sent to the LLM
# from growing without bound.
MAX_USED_WORDS = 500

# Rate limiter: cooldown per chat (seconds)
WORD_COOLDOWN = 10
_last_word_time: dict[int, float] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Redact sensitive env vars from log output
_SAFE_LOGGERS = ["httpx", "telegram", "httpcore"]


def _redact(val: str | None, full: bool = True) -> str:
    """Redact sensitive values for logging (show first 4 chars if not full)."""
    if not val:
        return "<unset>"
    if full or len(val) < 8:
        return "<redacted>"
    return val[:4] + "..."


logger.info("TELEGRAM_BOT_TOKEN=%s", _redact(TOKEN))
logger.info("BYNARA_API_KEY=%s", _redact(API_KEY))
logger.info("Model=%s, Allowlist=%s", MODEL, ALLOWED_IDS_RAW if ALLOWED_IDS_RAW else "open")

if not API_KEY:
    logger.error("BYNARA_API_KEY is not set — /word will fail")
if not TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN is not set — bot cannot start")

word_provider: WordProvider | None = None
if API_KEY:
    word_provider = WordProvider(API_KEY, BASE_URL, MODEL)


def _authorized(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


# The word/definition/etymology/examples/fun_fact fields come from the LLM
# and are dropped into a parse_mode="Markdown" message. If the model ever
# emits an unescaped *, _, [, or `, Telegram's legacy Markdown parser raises
# and the whole send silently fails. Escape those characters in any
# LLM-generated text before it's interpolated into our hand-written
# Markdown template.
_MD_SPECIAL_CHARS = ("*", "_", "[", "`")


def _escape_md(text: str) -> str:
    for ch in _MD_SPECIAL_CHARS:
        text = text.replace(ch, "\\" + ch)
    return text


def _read_state_unlocked() -> dict:
    defaults = {"used_words": [], "chat_ids": []}
    if not STATE_FILE.exists():
        return defaults
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return defaults | data
    except (json.JSONDecodeError, OSError):
        logger.warning("state.json corrupted, resetting")
        return defaults


def _write_state_unlocked(state: dict) -> None:
    if len(state.get("used_words", [])) > MAX_USED_WORDS:
        state["used_words"] = state["used_words"][-MAX_USED_WORDS:]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_state() -> dict:
    with STATE_LOCK:
        return _read_state_unlocked()


def save_state(state: dict) -> None:
    with STATE_LOCK:
        _write_state_unlocked(state)


def update_state(mutate) -> dict:
    """Atomically read, mutate, and persist state under a single lock.

    `mutate` is called with the current state dict and should modify it
    in place (or return a replacement dict).
    """
    with STATE_LOCK:
        state = _read_state_unlocked()
        result = mutate(state)
        state = result if result is not None else state
        _write_state_unlocked(state)
        return state


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_message.chat_id

    if not _authorized(chat_id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    def _register(state: dict) -> None:
        if chat_id not in state["chat_ids"]:
            state["chat_ids"].append(chat_id)
            logger.info("Registered chat: %s", chat_id)

    update_state(_register)

    await update.message.reply_text(
        "🌅 *Word of the Day Bot*\n\n"
        "I'll send you an interesting English word every day at *8 AM Nigeria time*.\n\n"
        "Commands:\n"
        "/word — get today's word immediately\n"
        "/stop — unsubscribe from daily words\n"
        "/start — re-register",
        parse_mode="Markdown",
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_message.chat_id
    was_subscribed = False

    def _unregister(state: dict) -> None:
        nonlocal was_subscribed
        if chat_id in state["chat_ids"]:
            state["chat_ids"].remove(chat_id)
            was_subscribed = True

    update_state(_unregister)

    if was_subscribed:
        logger.info("Unregistered chat: %s", chat_id)
        await update.message.reply_text("✅ Unsubscribed from daily words. Use /start to re-subscribe.")
    else:
        await update.message.reply_text("You're not subscribed.")


async def word_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_message.chat_id

    if not _authorized(chat_id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    # Rate limit
    now = time.monotonic()
    last = _last_word_time.get(chat_id, 0)
    if now - last < WORD_COOLDOWN:
        remaining = int(WORD_COOLDOWN - (now - last))
        await update.message.reply_text(f"⏳ Please wait {remaining}s before requesting another word.")
        return
    _last_word_time[chat_id] = now

    def _register(state: dict) -> None:
        if chat_id not in state["chat_ids"]:
            state["chat_ids"].append(chat_id)

    update_state(_register)

    msg = await update.message.reply_text("🔍 Digging up today's word...")
    await _send_word(chat_id, context)
    await msg.delete()


async def _send_word(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if word_provider is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Word generation is not configured (BYNARA_API_KEY missing).",
        )
        return

    state = load_state()
    used = state["used_words"]

    word_data = await word_provider.generate_word(used)
    if not word_data:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Couldn't generate a word today. Check the logs.",
        )
        return

    word = word_data.get("word", "?").strip()
    pronunciation = word_data.get("pronunciation", "")
    pos = word_data.get("pos", "")
    definition = word_data.get("definition", "")
    etymology = word_data.get("etymology", "")
    examples = word_data.get("examples", [])
    fun_fact = word_data.get("fun_fact", "")

    def _record_word(state: dict) -> None:
        state["used_words"].append(word.lower())

    update_state(_record_word)

    lines = [
        f"*WORD OF THE DAY*",
        f"{datetime.now(TZ).strftime('%B %d, %Y')}",
        "",
        f"📝 *Word:* {_escape_md(word)}",
    ]
    if pronunciation:
        lines.append(f"🔊 _{_escape_md(pronunciation)}_")
    if pos:
        lines.append(f"🏷️  {_escape_md(pos)}")
    if definition:
        lines.append(f"\n*Definition:*\n{_escape_md(definition)}")
    if etymology:
        lines.append(f"\n*Etymology:*\n{_escape_md(etymology)}")
    if examples:
        lines.append("\n*Examples:*")
        for i, ex in enumerate(examples[:3], 1):
            lines.append(f"{i}. {_escape_md(ex)}")
    if fun_fact:
        lines.append(f"\n💡 {_escape_md(fun_fact)}")

    text = "\n".join(lines)

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Failed to send to %s: %s", chat_id, e)


async def daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    chat_ids = state.get("chat_ids", [])
    if not chat_ids:
        logger.info("No registered users — nothing to send")
        return

    for chat_id in chat_ids:
        try:
            await _send_word(chat_id, context)
        except Exception as e:
            logger.error("Failed daily send to %s: %s", chat_id, e)


def main() -> None:
    if not TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN is not set — cannot start polling")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("word", word_now))

    app.job_queue.run_daily(
        daily_job,
        time=dtime(hour=8, minute=0, tzinfo=TZ),
    )

    logger.info("Bot started. Scheduled for 8 AM Africa/Lagos daily.")
    app.run_polling(allowed_updates=Update.MESSAGE)


if __name__ == "__main__":
    main()