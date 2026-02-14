import os
import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
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
if OWNER_ID == 0:
    raise RuntimeError("Missing/invalid OWNER_ID env var")

# -------------------- DB --------------------
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
conn.autocommit = True

# -------------------- CACHES (fast) --------------------
muted_cache: set[int] = set()
banned_cache: set[int] = set()
bot_muted_chats_cache: set[int] = set()  # chats where all bot/via_bot messages get deleted

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
        cur.execute("""
        CREATE TABLE IF NOT EXISTS known_chats (
            chat_id BIGINT PRIMARY KEY
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_muted_chats (
            chat_id BIGINT PRIMARY KEY
        );
        """)

def load_caches():
    global muted_cache, banned_cache, bot_muted_chats_cache
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT user_id FROM muted_users")
        muted_cache = {int(r["user_id"]) for r in cur.fetchall()}

        cur.execute("SELECT user_id FROM banned_users")
        banned_cache = {int(r["user_id"]) for r in cur.fetchall()}

        cur.execute("SELECT chat_id FROM bot_muted_chats")
        bot_muted_chats_cache = {int(r["chat_id"]) for r in cur.fetchall()}

def add_known_chat(chat_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO known_chats (chat_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (chat_id,),
        )

def get_all_known_chats() -> list[int]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT chat_id FROM known_chats")
        return [int(r["chat_id"]) for r in cur.fetchall()]

def add_mute(target_id: int):
    muted_cache.add(target_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO muted_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (target_id,),
        )

def remove_mute(target_id: int):
    muted_cache.discard(target_id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM muted_users WHERE user_id=%s", (target_id,))

def add_ban(user_id: int):
    banned_cache.add(user_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO banned_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (user_id,),
        )

def remove_ban(user_id: int):
    banned_cache.discard(user_id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM banned_users WHERE user_id=%s", (user_id,))

def add_bot_mute_chat(chat_id: int):
    bot_muted_chats_cache.add(chat_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bot_muted_chats (chat_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (chat_id,),
        )

def remove_bot_mute_chat(chat_id: int):
    bot_muted_chats_cache.discard(chat_id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM bot_muted_chats WHERE chat_id=%s", (chat_id,))

# -------------------- HELPERS --------------------
def owner_only(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == OWNER_ID)

def parse_id_arg(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not context.args or len(context.args) != 1:
        return None
    try:
        return int(context.args[0])
    except ValueError:
        return None

def message_ids_to_check(update: Update) -> list[int]:
    """
    IDs that may represent the sender "entity" for moderation checks:
    - from_user.id: normal user/bot sender
    - via_bot.id: message posted via a bot
    - sender_chat.id: channel-as-sender OR anonymous admin
    """
    msg = update.effective_message
    ids: list[int] = []
    if not msg:
        return ids

    if msg.from_user:
        ids.append(msg.from_user.id)
    if msg.via_bot:
        ids.append(msg.via_bot.id)
    if msg.sender_chat:
        ids.append(msg.sender_chat.id)

    return ids

# -------------------- COMMANDS --------------------
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    target_id = parse_id_arg(context)
    if target_id is None:
        await update.message.reply_text("Usage: /mute <id>")
        return
    add_mute(target_id)
    await update.message.reply_text(f"✅ Muted (global): {target_id}")

async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    target_id = parse_id_arg(context)
    if target_id is None:
        await update.message.reply_text("Usage: /unmute <id>")
        return
    remove_mute(target_id)
    await update.message.reply_text(f"✅ Unmuted (global): {target_id}")

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    user_id = parse_id_arg(context)
    if user_id is None:
        await update.message.reply_text("Usage: /ban <userid>")
        return

    add_ban(user_id)

    chats = get_all_known_chats()
    ok, fail = 0, 0
    for chat_id in chats:
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            ok += 1
        except Exception:
            fail += 1

    await update.message.reply_text(f"✅ Banned: {user_id} (ok:{ok} fail:{fail})")

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    user_id = parse_id_arg(context)
    if user_id is None:
        await update.message.reply_text("Usage: /unban <userid>")
        return

    remove_ban(user_id)

    chats = get_all_known_chats()
    ok, fail = 0, 0
    for chat_id in chats:
        try:
            await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            ok += 1
        except Exception:
            fail += 1

    await update.message.reply_text(f"✅ Unbanned: {user_id} (ok:{ok} fail:{fail})")

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    user_id = parse_id_arg(context)
    if user_id is None:
        await update.message.reply_text("Usage: /admin <userid>")
        return

    chats = get_all_known_chats()
    ok, fail = 0, 0
    for chat_id in chats:
        try:
            await context.bot.promote_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                can_manage_chat=True,
                can_delete_messages=True,
                can_manage_video_chats=True,
                can_restrict_members=True,
                can_promote_members=True,
                can_change_info=True,
                can_invite_users=True,
                can_pin_messages=True,
                can_manage_topics=True,
                # channel-specific (ignored where not applicable)
                can_post_messages=True,
                can_edit_messages=True,
            )
            ok += 1
        except Exception:
            fail += 1

    await update.message.reply_text(f"✅ Admin attempted for {user_id} (ok:{ok} fail:{fail})")

async def cmd_mutebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    chat_id = parse_id_arg(context)
    if chat_id is None:
        await update.message.reply_text("Usage: /mutebot <chat_id>")
        return
    add_bot_mute_chat(chat_id)
    await update.message.reply_text(f"✅ Bot-Mute aktiv in Chat: {chat_id}")

async def cmd_unmutebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    chat_id = parse_id_arg(context)
    if chat_id is None:
        await update.message.reply_text("Usage: /unmutebot <chat_id>")
        return
    remove_bot_mute_chat(chat_id)
    await update.message.reply_text(f"✅ Bot-Mute deaktiviert in Chat: {chat_id}")

async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: shows current chat id (useful for /mutebot)."""
    if not owner_only(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    await update.message.reply_text(f"chat_id: {chat.id} | type: {chat.type}")

# -------------------- DEBUG COMMANDS --------------------
async def cmd_testdelete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    if not update.message or not update.message.reply_to_message:
        await update.message.reply_text("Nutze /testdelete als Antwort auf eine Nachricht.")
        return
    try:
        await update.message.reply_to_message.delete()
        await update.message.reply_text("✅ testdelete: gelöscht")
    except Exception as e:
        await update.message.reply_text(f"❌ testdelete failed: {type(e).__name__}: {e}")

async def cmd_dbg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    if not update.message or not update.message.reply_to_message:
        await update.message.reply_text("Nutze /dbg als Antwort auf eine Nachricht.")
        return

    r = update.message.reply_to_message
    chat = r.chat
    from_id = r.from_user.id if r.from_user else None
    from_is_bot = r.from_user.is_bot if r.from_user else None
    via_id = r.via_bot.id if r.via_bot else None
    sender_chat_id = r.sender_chat.id if r.sender_chat else None

    await update.message.reply_text(
        "DBG:\n"
        f"- chat_id: {chat.id}\n"
        f"- chat_type: {chat.type}\n"
        f"- mutebot_active_here: {chat.id in bot_muted_chats_cache}\n"
        f"- from_user.id: {from_id}\n"
        f"- from_user.is_bot: {from_is_bot}\n"
        f"- via_bot.id: {via_id}\n"
        f"- sender_chat.id: {sender_chat_id}\n"
    )

# -------------------- MAIN HANDLER --------------------
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message

    if not chat or not msg:
        return

    # Track groups/supergroups/channels we see
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        add_known_chat(chat.id)

    # Only moderate in groups/supergroups
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    # ✅ Delete EVERYTHING sent as sender_chat (anonymous admin + channel-as-sender)
    if msg.sender_chat is not None:
        try:
            await msg.delete()
        except Exception:
            pass
        return

    # ✅ Per-group "mute all bots" (delete all bot/via_bot messages) if enabled for this chat
    if chat.id in bot_muted_chats_cache:
        try:
            # Bots writing directly
            if msg.from_user and msg.from_user.is_bot:
                await msg.delete()
                return
            # Messages posted via bots (inline/via_bot)
            if msg.via_bot is not None:
                await msg.delete()
                return
        except Exception:
            pass

    ids = message_ids_to_check(update)

    # Global ban (requires a real user_id; we can only ban from_user)
    if msg.from_user and msg.from_user.id in banned_cache:
        try:
            await context.bot.ban_chat_member(chat_id=chat.id, user_id=msg.from_user.id)
        except Exception:
            pass
        return

    # If message was posted via a banned bot -> delete it
    if msg.via_bot and msg.via_bot.id in banned_cache:
        try:
            await msg.delete()
        except Exception:
            pass
        return

    # Global mute: delete message if sender OR via_bot OR sender_chat (but sender_chat already handled) is muted
    if any(i in muted_cache for i in ids):
        try:
            await msg.delete()
        except Exception:
            pass
        return

# -------------------- START --------------------
def main():
    db_init()
    load_caches()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("admin", cmd_admin))

    app.add_handler(CommandHandler("mutebot", cmd_mutebot))
    app.add_handler(CommandHandler("unmutebot", cmd_unmutebot))
    app.add_handler(CommandHandler("chatid", cmd_chatid))

    # Debug
    app.add_handler(CommandHandler("testdelete", cmd_testdelete))
    app.add_handler(CommandHandler("dbg", cmd_dbg))

    app.add_handler(MessageHandler(filters.ALL, handle_all_messages))

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
