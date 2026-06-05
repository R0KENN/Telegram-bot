import time

import aiosqlite

from config import DB_PATH, DEFAULTS


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                chat_id INTEGER PRIMARY KEY, title TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS members (
                chat_id INTEGER, user_id INTEGER, full_name TEXT,
                username TEXT, joined_at INTEGER,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER, key TEXT, value TEXT,
                PRIMARY KEY (chat_id, key)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, text TEXT, btn_text TEXT, btn_url TEXT,
                publish_at INTEGER, status TEXT DEFAULT 'pending'
            )
        """)
        await db.commit()


async def register_channel(chat_id, title):
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем, есть ли уже такой канал
        async with db.execute(
            "SELECT 1 FROM channels WHERE chat_id=?", (chat_id,)
        ) as cur:
            exists = await cur.fetchone() is not None

        # Сохраняем/обновляем название канала
        await db.execute(
            "INSERT OR REPLACE INTO channels (chat_id, title) VALUES (?, ?)",
            (chat_id, title)
        )

        # Если канал новый — явно выключаем автоприём по умолчанию
        if not exists:
            await db.execute(
                "INSERT OR IGNORE INTO settings (chat_id, key, value) "
                "VALUES (?, 'auto_approve', '0')",
                (chat_id,)
            )

        await db.commit()


async def get_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id, title FROM channels") as cur:
            return await cur.fetchall()


async def get_channel_title(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT title FROM channels WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else "канал"


async def get_setting(chat_id, key):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE chat_id=? AND key=?", (chat_id, key)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else DEFAULTS.get(key)


async def set_setting(chat_id, key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (chat_id, key, value) VALUES (?, ?, ?)",
            (chat_id, key, value)
        )
        await db.commit()


async def reset_settings(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM settings WHERE chat_id=?", (chat_id,))
        await db.commit()


async def is_auto_approve(chat_id):
    return await get_setting(chat_id, "auto_approve") == "1"


async def save_member(chat_id, user_id, full_name, username):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO members VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_id, full_name, username, int(time.time()))
        )
        await db.commit()


async def count_members(chat_id, since=None):
    q = "SELECT COUNT(*) FROM members WHERE chat_id=?"
    params = [chat_id]
    if since is not None:
        q += " AND joined_at>=?"
        params.append(since)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(q, params) as cur:
            return (await cur.fetchone())[0]


async def get_last_members(chat_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT full_name, username, joined_at FROM members "
            "WHERE chat_id=? ORDER BY joined_at DESC LIMIT ?", (chat_id, limit)
        ) as cur:
            return await cur.fetchall()


async def get_member_ids(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM members WHERE chat_id=?", (chat_id,)) as cur:
            return [r[0] for r in await cur.fetchall()]


async def add_post(chat_id, text, btn_text, btn_url, publish_at):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO posts (chat_id, text, btn_text, btn_url, publish_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, text, btn_text, btn_url, publish_at)
        )
        await db.commit()


async def get_pending_posts(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, text, publish_at FROM posts "
            "WHERE chat_id=? AND status='pending' ORDER BY publish_at", (chat_id,)
        ) as cur:
            return await cur.fetchall()


async def get_due_posts():
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, chat_id, text, btn_text, btn_url FROM posts "
            "WHERE status='pending' AND publish_at<=?", (now,)
        ) as cur:
            return await cur.fetchall()


async def mark_post(post_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE posts SET status=? WHERE id=?", (status, post_id))
        await db.commit()


async def cancel_post(post_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE posts SET status='cancelled' WHERE id=? AND chat_id=?",
            (post_id, chat_id)
        )
        await db.commit()
