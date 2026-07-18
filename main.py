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
MODEL = os.getenv("BYNARA_MODEL", "mimo-v2.5-free")

# Allowlist — comma-separated chat IDs, empty = open to all
ALLOWED_IDS_RAW = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = {int(x) for x in ALLOWED_IDS_RAW.split(",") if x.strip().isdigit()}

STATE_FILE = Path(__file__).parent / "state.json"
STATE_LOCK = threading.Lock()
TZ = zoneinfo.ZoneInfo("Africa/Lagos")

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


def load_state() -> dict:
    defaults = {"used_words": [], "chat_ids": []}
    if not STATE_FILE.exists():
        return defaults
    try:
        with STATE_LOCK:
            with open(STATE_FILE) as f:
                data = json.load(f)
        return defaults | data
    except (json.JSONDecodeError, OSError):
        logger.warning("state.json corrupted, resetting")
        return defaults


def save_state(state: dict) -> None:
    with STATE_LOCK:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_message.chat_id

    if not _authorized(chat_id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    state = load_state()
    if chat_id not in state["chat_ids"]:
        state["chat_ids"].append(chat_id)
        save_state(state)
        logger.info("Registered chat: %s", chat_id)

    await update.message.reply_text(
        "🌅 *Word of the Day Bot*\n\n"
        "I'll send you an obscure English word every day at *8 AM Nigeria time*.\n\n"
        "Commands:\n"
        "/word — get today's word immediately\n"
        "/stop — unsubscribe from daily words\n"
        "/start — re-register",
        parse_mode="Markdown",
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_message.chat_id
    state = load_state()
    if chat_id in state["chat_ids"]:
        state["chat_ids"].remove(chat_id)
        save_state(state)
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

    state = load_state()
    if chat_id not in state["chat_ids"]:
        state["chat_ids"].append(chat_id)
        save_state(state)

    msg = await update.message.reply_text("🔍 Digging up an obscure word...")
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

    state["used_words"].append(word.lower())
    save_state(state)

    lines = [
        f"*WORD OF THE DAY*",
        f"{datetime.now(TZ).strftime('%B %d, %Y')}",
        "",
        f"📝 *Word:* {word}",
    ]
    if pronunciation:
        lines.append(f"🔊 _{pronunciation}_")
    if pos:
        lines.append(f"🏷️  {pos}")
    if definition:
        lines.append(f"\n*Definition:*\n{definition}")
    if etymology:
        lines.append(f"\n*Etymology:*\n{etymology}")
    if examples:
        lines.append("\n*Examples:*")
        for i, ex in enumerate(examples[:3], 1):
            lines.append(f"{i}. {ex}")
    if fun_fact:
        lines.append(f"\n💡 {fun_fact}")

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