import os
import re

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
OWNER_ID = int(os.getenv("OWNER_ID", "0").strip() or "0")

if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")
if OWNER_ID == 0:
    raise RuntimeError("Missing/invalid OWNER_ID env var")

# -------------------- SETTINGS --------------------
# Matches "#1", "#2", "#123" etc.
HASH_NUMBER_RE = re.compile(r"#\d+", re.IGNORECASE)

# Any digit anywhere in name/username
ANY_DIGIT_RE = re.compile(r"\d")

# "ADS" anywhere in a word (case-insensitive). This will match e.g. "xADSx", "myadsaccount"
ADS_RE = re.compile(r"ads", re.IGNORECASE)

# Tide-mode per chat (in-memory; resets on restart)
tide_chats: set[int] = set()


# -------------------- HELPERS --------------------
def owner_only(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == OWNER_ID)


def user_full_text(u) -> str:
    # Combine name + username for detection
    parts = []
    if getattr(u, "first_name", None):
        parts.append(u.first_name)
    if getattr(u, "last_name", None):
        parts.append(u.last_name)
    if getattr(u, "username", None):
        parts.append(u.username)
    return " ".join(parts).strip()


def is_suspicious_name(name: str) -> bool:
    if not name:
        return False
    n = name.strip()

    # Condition 1: "#<number>" anywhere
    if HASH_NUMBER_RE.search(n):
        return True

    # Condition 2: ANY digit anywhere
    if ANY_DIGIT_RE.search(n):
        return True

    # Condition 3: "ADS" anywhere
    if ADS_RE.search(n):
        return True

    return False


async def is_admin_or_owner(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """
    Prevent banning admins/creator (and also prevent banning OWNER_ID).
    """
    if user_id == OWNER_ID:
        return True
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        # If we can't fetch status, be conservative: don't treat as admin
        return False


async def ban_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception:
        pass


# -------------------- COMMANDS --------------------
async def cmd_tide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /tide toggles tide-mode in the current group/supergroup.
    When ON: every new message from normal users -> ban.
    """
    if not owner_only(update):
        return

    chat = update.effective_chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        if update.message:
            await update.message.reply_text("Nutze /tide in einer Gruppe/Supergroup.")
        return

    if chat.id in tide_chats:
        tide_chats.remove(chat.id)
        await update.message.reply_text("🌊 Tide: AUS (neue Posts werden NICHT mehr auto-gebannt).")
    else:
        tide_chats.add(chat.id)
        await update.message.reply_text("🌊 Tide: AN (jeder neue Post von Usern -> BAN).")


# -------------------- MAIN HANDLER --------------------
async def handle_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message

    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    # 1) Handle new joins: ban on join if name suspicious
    if msg and msg.new_chat_members:
        for u in msg.new_chat_members:
            try:
                if await is_admin_or_owner(context, chat.id, u.id):
                    continue
                name = user_full_text(u)
                if is_suspicious_name(name):
                    await ban_user(context, chat.id, u.id)
            except Exception:
                pass
        return

    # Only process normal messages below
    if not msg or not msg.from_user:
        return

    user = msg.from_user

    # Never touch owner/admins (avoid locking you out)
    if await is_admin_or_owner(context, chat.id, user.id):
        return

    # 2) Tide mode: ban everyone who posts (normal users)
    if chat.id in tide_chats:
        try:
            await msg.delete()
        except Exception:
            pass
        await ban_user(context, chat.id, user.id)
        return

    # 3) Normal mode: ban if suspicious name
    name = user_full_text(user)
    if is_suspicious_name(name):
        try:
            await msg.delete()
        except Exception:
            pass
        await ban_user(context, chat.id, user.id)
        return


# -------------------- START --------------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("tide", cmd_tide))
    app.add_handler(MessageHandler(filters.ALL, handle_all_updates))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
