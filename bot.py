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

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0").strip() or "0")

if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL env var")
if OWNER_ID == 0:
    raise RuntimeError("Missing/invalid OWNER_ID env var")

# --- DB setup (single connection; ok for small/medium bots) ---
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
conn.autocommit = True

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

def is_muted(user_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM muted_users WHERE user_id=%s", (user_id,))
        return cur.fetchone() is not None

def add_mute(user_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO muted_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (user_id,),
        )

def remove_mute(user_id: int):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM muted_users WHERE user_id=%s", (user_id,))

def is_banned(user_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM banned_users WHERE user_id=%s", (user_id,))
        return cur.fetchone() is not None

def add_ban(user_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO banned_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (user_id,),
        )

def remove_ban(user_id: int):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM banned_users WHERE user_id=%s", (user_id,))

def owner_only(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == OWNER_ID)

def parse_user_id_arg(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not context.args or len(context.args) != 1:
        return None
    try:
        return int(context.args[0])
    except ValueError:
        return None

# --- Commands ---
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    user_id = parse_user_id_arg(context)
    if user_id is None:
        await update.message.reply_text("Usage: /mute <userid>")
        return
    add_mute(user_id)
    await update.message.reply_text(f"✅ Muted: {user_id}")

async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    user_id = parse_user_id_arg(context)
    if user_id is None:
        await update.message.reply_text("Usage: /unmute <userid>")
        return
    remove_mute(user_id)
    await update.message.reply_text(f"✅ Unmuted: {user_id}")

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    user_id = parse_user_id_arg(context)
    if user_id is None:
        await update.message.reply_text("Usage: /ban <userid>")
        return

    add_ban(user_id)

    # Try to ban in all known chats (best-effort)
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
    user_id = parse_user_id_arg(context)
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
    user_id = parse_user_id_arg(context)
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
                # Channel-specific permissions (ignored in groups where not applicable):
                can_post_messages=True,
                can_edit_messages=True,
            )
            ok += 1
        except Exception:
            fail += 1

    await update.message.reply_text(f"✅ Admin attempted for {user_id} (ok:{ok} fail:{fail})")

# --- Main message handler (mute/ban enforcement + chat discovery) ---
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    if not chat or not msg or not user:
        return

    # Track groups/supergroups/channels we see
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        add_known_chat(chat.id)

    # Only enforce in group/supergroup (messages in channels are different)
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    uid = user.id

    # If globally banned: try to ban in this chat immediately
    if is_banned(uid):
        try:
            await context.bot.ban_chat_member(chat_id=chat.id, user_id=uid)
        except Exception:
            pass
        return

    # If muted: delete the new message
    if is_muted(uid):
        try:
            await msg.delete()
        except Exception:
            pass

def main():
    db_init()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("admin", cmd_admin))

    app.add_handler(MessageHandler(filters.ALL, handle_all_messages))

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
