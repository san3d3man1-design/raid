import os
import re
import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update
from telegram.constants import ChatType, MessageEntityType
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# -------------------- ENV --------------------
TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0").strip() or "0")

if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL env var")

# -------------------- DB --------------------
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
conn.autocommit = True

muted_cache: set[int] = set()
banned_cache: set[int] = set()

# -------------------- BAD WORDS --------------------
BASE_BAD_WORDS = [
    "fake",
    "uebernommen",
    "gehackt",
    "emre",
    "achtung",
    "use",
]

LINK_RE = re.compile(
    r"(?i)\b("
    r"https?://\S+|"
    r"t\.me/\S+|"
    r"telegram\.me/\S+|"
    r"www\.\S+|"
    r"\S+\.(?:com|net|org|de|ru|xyz|io|gg|me|tv|app|site|shop|store|info)\b"
    r")"
)

# -------------------- DB INIT --------------------
def db_init():
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS muted_users (
            user_id BIGINT PRIMARY KEY
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id BIGINT PRIMARY KEY
        );
        """)

def load_caches():
    global muted_cache, banned_cache
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT user_id FROM muted_users")
        muted_cache = {int(r["user_id"]) for r in cur.fetchall()}

        cur.execute("SELECT user_id FROM banned_users")
        banned_cache = {int(r["user_id"]) for r in cur.fetchall()}

def add_mute(user_id: int):
    muted_cache.add(user_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO muted_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (user_id,),
        )

def add_ban(user_id: int):
    banned_cache.add(user_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO banned_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (user_id,),
        )

# -------------------- TEXT NORMALIZATION --------------------
def normalize_text(text: str) -> str:
    t = (text or "").lower()
    t = (
        t.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )
    t = re.sub(r"(.)\1{1,}", r"\1", t)
    t = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
    return t

def contains_bad_word(text: str) -> bool:
    t = normalize_text(text)
    for w in BASE_BAD_WORDS:
        if w in t:
            return True
    return False

def message_has_link(msg) -> bool:
    entities = []
    if msg.entities:
        entities.extend(msg.entities)
    if msg.caption_entities:
        entities.extend(msg.caption_entities)

    for e in entities:
        if e.type in (MessageEntityType.URL, MessageEntityType.TEXT_LINK):
            return True

    content = (msg.text or "") + "\n" + (msg.caption or "")
    return bool(LINK_RE.search(content))

# -------------------- COMMANDS --------------------
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Antworte auf eine Nachricht mit /mute")
        return

    target = update.message.reply_to_message.from_user
    if not target:
        return

    add_mute(target.id)

    try:
        await update.message.reply_to_message.delete()
    except:
        pass

    await update.message.reply_text(f"🔇 User gemutet: {target.id}")

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Antworte auf eine Nachricht mit /ban")
        return

    target = update.message.reply_to_message.from_user
    if not target:
        return

    add_ban(target.id)

    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
    except:
        pass

    await update.message.reply_text(f"🚫 User gebannt: {target.id}")

# -------------------- MAIN HANDLER --------------------
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message

    if not chat or not msg:
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not msg.from_user:
        return

    user_id = msg.from_user.id

    # Banned
    if user_id in banned_cache:
        try:
            await context.bot.ban_chat_member(chat.id, user_id)
        except:
            pass
        return

    # Muted
    if user_id in muted_cache:
        try:
            await msg.delete()
        except:
            pass
        return

    content = (msg.text or "") + "\n" + (msg.caption or "")

    if contains_bad_word(content) or message_has_link(msg):
        add_mute(user_id)
        try:
            await msg.delete()
        except:
            pass

# -------------------- START --------------------
def main():
    db_init()
    load_caches()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(MessageHandler(filters.ALL, handle_all_messages))

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
