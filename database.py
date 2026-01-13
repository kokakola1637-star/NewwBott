# database.py
import aiosqlite

DB_NAME = "bot_data.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS verified_users (
                user_id INTEGER PRIMARY KEY,
                verified INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS auto_mode_users (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sent_videos (
                video_id_or_url TEXT PRIMARY KEY,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def is_user_verified(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT verified FROM verified_users WHERE user_id = ?",
            (user_id,)
        )
        row = await cur.fetchone()
        return bool(row and row[0])

async def verify_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO verified_users (user_id, verified) VALUES (?, 1)",
            (user_id,)
        )
        await db.commit()

async def set_auto_mode(user_id: int, enabled: bool):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO auto_mode_users (user_id, enabled) VALUES (?, ?)",
            (user_id, int(enabled))
        )
        await db.commit()

async def is_any_auto_enabled() -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT 1 FROM auto_mode_users WHERE enabled = 1 LIMIT 1"
        )
        return await cur.fetchone() is not None

async def is_video_sent(video_id: str) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT 1 FROM sent_videos WHERE video_id_or_url = ?",
            (video_id,)
        )
        return await cur.fetchone() is not None

async def save_sent_video(video_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO sent_videos (video_id_or_url) VALUES (?)",
                (video_id,)
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            pass