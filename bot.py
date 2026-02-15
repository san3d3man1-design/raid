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

# Lock title (global /lockinfo)
locked_info_cache: dict[int, dict[str, str | None]] = {}

# Enforce "NO chat photo" (groups + channels) when enabled
no_photo_chats_cache: set[int] = set()

# Global broadcast lock (groups + channels)
broadcast_lock_global: bool = False

# Global clean info-events (delete “title/photo changed” service messages)
clean_info_global: bool = False

# Bot id cache (for "only my bot may post")
BOT_ID_CACHE: int | None = None

# Watch/debug (to verify whether we receive updates for certain messages)
watch_chats: set[int] = set()
watch_left: dict[int, int] = {}


# -------------------- DB INIT / LOAD --------------------
def db_init():
    with conn.cursor() as cur:
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS muted_users (
            user_id BIGINT PRIMARY KEY
        );
        """
        )
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id BIGINT PRIMARY KEY
        );
        """
        )
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS known_chats (
            chat_id BIGINT PRIMARY KEY
        );
        """
        )
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS bot_muted_chats (
            chat_id BIGINT PRIMARY KEY
        );
        """
        )

        # Locked chat info (title + (legacy) photo_file_id)
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS locked_group_info (
            chat_id BIGINT PRIMARY KEY,
            title TEXT,
            photo_file_id TEXT
        );
        """
        )

        # Chats where chat photo should always be deleted
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS no_photo_chats (
            chat_id BIGINT PRIMARY KEY
        );
        """
        )

        # Global broadcast lock state (single row)
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS broadcast_lock_state (
            id SMALLINT PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT FALSE
        );
        """
        )
        cur.execute(
            """
        INSERT INTO broadcast_lock_state (id, enabled)
        VALUES (1, FALSE)
        ON CONFLICT (id) DO NOTHING;
        """
        )

        # Global clean info events state (single row)
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS clean_info_state (
            id SMALLINT PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT FALSE
        );
        """
        )
        cur.execute(
            """
        INSERT INTO clean_info_state (id, enabled)
        VALUES (1, FALSE)
        ON CONFLICT (id) DO NOTHING;
        """
        )


def load_caches():
    global muted_cache, banned_cache, bot_muted_chats_cache, locked_info_cache, no_photo_chats_cache
    global broadcast_lock_global, clean_info_global

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT user_id FROM muted_users")
        muted_cache = {int(r["user_id"]) for r in cur.fetchall()}

        cur.execute("SELECT user_id FROM banned_users")
        banned_cache = {int(r["user_id"]) for r in cur.fetchall()}

        cur.execute("SELECT chat_id FROM bot_muted_chats")
        bot_muted_chats_cache = {int(r["chat_id"]) for r in cur.fetchall()}

        cur.execute("SELECT chat_id, title, photo_file_id FROM locked_group_info")
        rows = cur.fetchall()
        locked_info_cache = {
            int(r["chat_id"]): {"title": r["title"], "photo_file_id": r["photo_file_id"]}
            for r in rows
        }

        cur.execute("SELECT chat_id FROM no_photo_chats")
        no_photo_chats_cache = {int(r["chat_id"]) for r in cur.fetchall()}

        cur.execute("SELECT enabled FROM broadcast_lock_state WHERE id=1")
        row = cur.fetchone()
        broadcast_lock_global = bool(row["enabled"]) if row else False

        cur.execute("SELECT enabled FROM clean_info_state WHERE id=1")
        row = cur.fetchone()
        clean_info_global = bool(row["enabled"]) if row else False


def set_broadcast_lock_state(enabled: bool):
    global broadcast_lock_global
    broadcast_lock_global = enabled
    with conn.cursor() as cur:
        cur.execute("UPDATE broadcast_lock_state SET enabled=%s WHERE id=1", (enabled,))


def set_clean_info_state(enabled: bool):
    global clean_info_global
    clean_info_global = enabled
    with conn.cursor() as cur:
        cur.execute("UPDATE clean_info_state SET enabled=%s WHERE id=1", (enabled,))


# -------------------- DB HELPERS --------------------
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


def upsert_locked_info(chat_id: int, title: str | None, photo_file_id: str | None):
    locked_info_cache[chat_id] = {"title": title, "photo_file_id": photo_file_id}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO locked_group_info (chat_id, title, photo_file_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (chat_id)
            DO UPDATE SET title=EXCLUDED.title, photo_file_id=EXCLUDED.photo_file_id
            """,
            (chat_id, title, photo_file_id),
        )


def delete_locked_info(chat_id: int):
    locked_info_cache.pop(chat_id, None)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM locked_group_info WHERE chat_id=%s", (chat_id,))


def add_no_photo_chat(chat_id: int):
    no_photo_chats_cache.add(chat_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO no_photo_chats (chat_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (chat_id,),
        )


def remove_no_photo_chat(chat_id: int):
    no_photo_chats_cache.discard(chat_id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM no_photo_chats WHERE chat_id=%s", (chat_id,))


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


def message_ids_to_check(msg) -> list[int]:
    """
    IDs that may represent the sender "entity" for moderation checks:
    - from_user.id: normal user/bot sender
    - via_bot.id: message posted via a bot
    - sender_chat.id: channel-as-sender OR anonymous admin
    """
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


async def bot_can_change_info(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    try:
        me = await context.bot.get_me()
        m = await context.bot.get_chat_member(chat_id=chat_id, user_id=me.id)

        if getattr(m, "status", None) not in ("administrator", "creator"):
            return False
        if getattr(m, "status", None) == "creator":
            return True
        return bool(getattr(m, "can_change_info", False))
    except Exception:
        return False


async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=text)
    except Exception:
        pass


async def enforce_no_chat_photo(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        await context.bot.delete_chat_photo(chat_id)
    except Exception as e:
        await notify_owner(context, f"❌ delete_chat_photo failed in chat {chat_id}: {type(e).__name__}: {e}")


async def get_bot_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    global BOT_ID_CACHE
    if BOT_ID_CACHE is None:
        me = await context.bot.get_me()
        BOT_ID_CACHE = me.id
    return BOT_ID_CACHE


async def is_message_from_this_bot(context: ContextTypes.DEFAULT_TYPE, msg) -> bool:
    bot_id = await get_bot_id(context)
    if msg.from_user and msg.from_user.is_bot and msg.from_user.id == bot_id:
        return True
    if msg.via_bot and msg.via_bot.id == bot_id:
        return True
    return False


def is_broadcast_like(msg) -> bool:
    if getattr(msg, "sender_chat", None) is not None:
        return True
    if bool(getattr(msg, "is_automatic_forward", False)):
        return True
    fchat = getattr(msg, "forward_from_chat", None)
    if fchat and getattr(fchat, "type", None) == ChatType.CHANNEL:
        return True
    return False


def is_info_change_service_message(msg) -> bool:
    return bool(
        getattr(msg, "new_chat_title", None)
        or getattr(msg, "new_chat_photo", None)
        or getattr(msg, "delete_chat_photo", None)
    )


# -------------------- CHANNEL ENFORCEMENT JOB --------------------
async def job_enforce_channels(context: ContextTypes.DEFAULT_TYPE):
    """
    Channels often don't send service-message updates for title/photo changes.
    So we periodically poll all known channels and enforce:
    - title lock (/lockinfo)
    - no photo (/locknophoto)
    """
    chats = get_all_known_chats()

    for chat_id in chats:
        try:
            chat = await context.bot.get_chat(chat_id)
            if chat.type != ChatType.CHANNEL:
                continue

            # Title lock
            if chat_id in locked_info_cache:
                desired = locked_info_cache[chat_id].get("title")
                if desired and chat.title != desired:
                    try:
                        await context.bot.set_chat_title(chat_id, desired)
                    except Exception:
                        pass

            # No photo enforcement
            if chat_id in no_photo_chats_cache:
                if chat.photo is not None:
                    try:
                        await context.bot.delete_chat_photo(chat_id)
                    except Exception:
                        pass

        except Exception:
            # bot removed / no permissions / chat not accessible
            pass


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
    if not owner_only(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    await update.message.reply_text(f"chat_id: {chat.id} | type: {chat.type}")


async def cmd_lockinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lock titles for ALL known groups/supergroups/channels where bot can change info."""
    if not owner_only(update):
        return

    chats = get_all_known_chats()
    ok, fail = 0, 0

    for chat_id in chats:
        try:
            chat = await context.bot.get_chat(chat_id)
            if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
                continue
            if not await bot_can_change_info(context, chat_id):
                continue

            title = chat.title
            photo_file_id = chat.photo.big_file_id if chat.photo else None  # legacy field
            upsert_locked_info(chat_id, title, photo_file_id)
            ok += 1
        except Exception:
            fail += 1

    await update.message.reply_text(f"🔒 LockInfo aktiv (Titel) | ok:{ok} fail:{fail}")


async def cmd_unlockinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    ids = list(locked_info_cache.keys())
    for chat_id in ids:
        delete_locked_info(chat_id)
    await update.message.reply_text("🔓 LockInfo deaktiviert.")


async def cmd_locknophoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enforce 'no chat photo' in ALL known groups+channels where bot can change info."""
    if not owner_only(update):
        return

    chats = get_all_known_chats()
    ok, fail = 0, 0

    for chat_id in chats:
        try:
            chat = await context.bot.get_chat(chat_id)
            if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
                continue
            if not await bot_can_change_info(context, chat_id):
                continue

            add_no_photo_chat(chat_id)

            if chat.photo is not None:
                await enforce_no_chat_photo(context, chat_id)

            ok += 1
        except Exception:
            fail += 1

    await update.message.reply_text(f"🧼 No-Photo aktiv (Gruppen+Kanäle) | ok:{ok} fail:{fail}")


async def cmd_unlocknophoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    ids = list(no_photo_chats_cache)
    for chat_id in ids:
        remove_no_photo_chat(chat_id)
    await update.message.reply_text("✅ No-Photo deaktiviert.")


async def cmd_lockbroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    set_broadcast_lock_state(True)
    await update.message.reply_text(
        "🔒 Globaler Broadcast-Lock aktiv: Nur der Bot darf posten (Kanäle), Broadcast wird überall gelöscht."
    )


async def cmd_unlockbroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    set_broadcast_lock_state(False)
    await update.message.reply_text("🔓 Globaler Broadcast-Lock deaktiviert.")


async def cmd_cleaninfo_global_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    set_clean_info_state(True)
    await update.message.reply_text("🧹 Global aktiv: Gruppeninfo-Änderungs-Mitteilungen werden automatisch gelöscht.")


async def cmd_cleaninfo_global_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    set_clean_info_state(False)
    await update.message.reply_text("✅ Global aus: Gruppeninfo-Änderungs-Mitteilungen werden nicht mehr automatisch gelöscht.")


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
        f"- lockinfo_title_active: {chat.id in locked_info_cache}\n"
        f"- nophoto_active: {chat.id in no_photo_chats_cache}\n"
        f"- broadcast_lock_global: {broadcast_lock_global}\n"
        f"- cleaninfo_global: {clean_info_global}\n"
        f"- from_user.id: {from_id}\n"
        f"- from_user.is_bot: {from_is_bot}\n"
        f"- via_bot.id: {via_id}\n"
        f"- sender_chat.id: {sender_chat_id}\n"
    )


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    chat = update.effective_chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        await update.message.reply_text("Nutze /watch in einer Gruppe/Supergroup/Kanal.")
        return
    watch_chats.add(chat.id)
    watch_left[chat.id] = 20
    await update.message.reply_text("👀 Watch aktiv: Ich logge die nächsten 20 Updates an dich privat.")


async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    watch_chats.discard(chat.id)
    watch_left.pop(chat.id, None)
    await update.message.reply_text("✅ Watch aus.")


# -------------------- MAIN HANDLER --------------------
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message

    if not chat or not msg:
        return

    # Track groups/supergroups/channels we see
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        add_known_chat(chat.id)

    # ---- WATCH DEBUG ----
    if chat.id in watch_chats:
        left = watch_left.get(chat.id, 0)
        if left > 0:
            fu = msg.from_user
            txt = (
                f"WATCH in {chat.id}:\n"
                f"- chat_type: {chat.type}\n"
                f"- from_user: {fu.id if fu else None}\n"
                f"- is_bot: {fu.is_bot if fu else None}\n"
                f"- via_bot: {msg.via_bot.id if msg.via_bot else None}\n"
                f"- sender_chat: {msg.sender_chat.id if msg.sender_chat else None}\n"
                f"- text: {(msg.text or msg.caption or '')[:80]}"
            )
            try:
                await context.bot.send_message(chat_id=OWNER_ID, text=txt)
            except Exception:
                pass
            watch_left[chat.id] = left - 1
        else:
            watch_chats.discard(chat.id)
            watch_left.pop(chat.id, None)

    # ---- TITLE LOCK (groups + channels) for groups works via service message ----
    if chat.id in locked_info_cache:
        locked = locked_info_cache[chat.id]
        if getattr(msg, "new_chat_title", None):
            try:
                if locked.get("title"):
                    await context.bot.set_chat_title(chat.id, locked["title"])
            except Exception as e:
                await notify_owner(context, f"❌ set_chat_title failed in chat {chat.id}: {type(e).__name__}: {e}")

    # ---- NO PHOTO ENFORCEMENT (groups + channels) for groups works via service message ----
    if chat.id in no_photo_chats_cache:
        if getattr(msg, "new_chat_photo", None) or getattr(msg, "delete_chat_photo", None):
            await enforce_no_chat_photo(context, chat.id)

    # ---- CLEAN INFO-EVENT MESSAGES (global) ----
    # Do this after revert actions, so locks still work.
    if clean_info_global and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        if is_info_change_service_message(msg):
            try:
                await msg.delete()
            except Exception:
                pass
            return

    # -------------------- GROUP MODERATION (unchanged) --------------------
    # Only moderate users in groups/supergroups (ban/mute/delete etc.)
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    # ✅ Delete EVERYTHING sent as sender_chat (anonymous admin + channel-as-sender) IN GROUPS
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

    ids = message_ids_to_check(msg)

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

    # Moderation commands
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("admin", cmd_admin))

    # Bot mute per chat
    app.add_handler(CommandHandler("mutebot", cmd_mutebot))
    app.add_handler(CommandHandler("unmutebot", cmd_unmutebot))
    app.add_handler(CommandHandler("chatid", cmd_chatid))

    # Title lock (global)
    app.add_handler(CommandHandler("lockinfo", cmd_lockinfo))
    app.add_handler(CommandHandler("unlockinfo", cmd_unlockinfo))

    # No-photo (global)
    app.add_handler(CommandHandler("locknophoto", cmd_locknophoto))
    app.add_handler(CommandHandler("unlocknophoto", cmd_unlocknophoto))

    # Broadcast lock (global) - kept, but not handled in message handler in this version
    app.add_handler(CommandHandler("lockbroadcast", cmd_lockbroadcast))
    app.add_handler(CommandHandler("unlockbroadcast", cmd_unlockbroadcast))

    # Clean info service messages (global)
    app.add_handler(CommandHandler("cleaninfo_global_on", cmd_cleaninfo_global_on))
    app.add_handler(CommandHandler("cleaninfo_global_off", cmd_cleaninfo_global_off))

    # Debug
    app.add_handler(CommandHandler("testdelete", cmd_testdelete))
    app.add_handler(CommandHandler("dbg", cmd_dbg))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))

    # Main stream
    app.add_handler(MessageHandler(filters.ALL, handle_all_messages))

    # NEW: Channel polling enforcement (every 60s)
    app.job_queue.run_repeating(job_enforce_channels, interval=60, first=10)

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
