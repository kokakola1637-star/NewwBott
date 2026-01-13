import asyncio
import aiosqlite
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime
from typing import List, Dict, Optional, Tuple

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bs4 import BeautifulSoup
from fastapi import FastAPI
import uvicorn
from urllib.parse import urljoin, urlparse

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_IDS_STR = os.getenv("GROUP_IDS", "-1001234567890,-1009876543210")
ADMIN_KEYWORD = os.getenv("ADMIN_KEYWORD", "Fxuniverse")
MAX_CONCURRENT_SENDS = 5  # Max messages sent at once (Telegram Limit safe)
RETRY_ATTEMPTS = 3

try:
    GROUP_IDS = [int(gid.strip()) for gid in GROUP_IDS_STR.split(',')]
except ValueError:
    logging.error("Invalid GROUP_IDS format. Using defaults.")
    GROUP_IDS = [-1001234567890, -1009876543210]

TARGET_AUTO = GROUP_IDS[0]
TARGET_MANUAL = GROUP_IDS[1]
BASE_URL = "https://lolpol2.com/"

# ==========================================
# DATABASE LAYER (Connection Pool Pattern)
# ==========================================
DB_NAME = "bot_pro.db"

class Database:
    def __init__(self):
        self.db_path = DB_NAME

    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("PRAGMA journal_mode=WAL") # Optimization for write speed
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self._init_tables()

    async def _init_tables(self):
        await self.conn.execute('''CREATE TABLE IF NOT EXISTS users (
                                    user_id INTEGER PRIMARY KEY, 
                                    is_verified BOOLEAN DEFAULT 0,
                                    join_date DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        
        await self.conn.execute('''CREATE TABLE IF NOT EXISTS state (
                                    key TEXT PRIMARY KEY, 
                                    value TEXT)''')
        
        await self.conn.execute('''CREATE TABLE IF NOT EXISTS history (
                                    post_id TEXT PRIMARY KEY, 
                                    title TEXT, 
                                    sent_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        
        await self.conn.execute('''CREATE TABLE IF NOT EXISTS analytics (
                                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                                    metric_name TEXT,
                                    value INTEGER,
                                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        await self.conn.commit()

    async def is_verified(self, user_id: int) -> bool:
        cursor = await self.conn.execute("SELECT is_verified FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def verify_user(self, user_id: int):
        await self.conn.execute('INSERT OR REPLACE INTO users (user_id, is_verified) VALUES (?, 1)', (user_id,))
        await self.conn.commit()

    async def get_mode(self) -> str:
        cursor = await self.conn.execute("SELECT value FROM state WHERE key = 'mode'")
        row = await cursor.fetchone()
        return row[0] if row else "off"

    async def set_mode(self, mode: str):
        await self.conn.execute('INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)', ('mode', mode))
        await self.conn.commit()

    async def is_sent(self, post_id: str) -> bool:
        cursor = await self.conn.execute("SELECT 1 FROM history WHERE post_id = ?", (post_id,))
        return await cursor.fetchone() is not None

    async def save_post(self, post_id: str, title: str):
        try:
            await self.conn.execute("INSERT INTO history (post_id, title) VALUES (?, ?)", (post_id, title))
            await self.conn.commit()
            # Update analytics
            await self.log_metric('posts_sent', 1)
        except aiosqlite.IntegrityError:
            pass

    async def log_metric(self, name: str, value: int):
        # Increment a counter in analytics
        await self.conn.execute("INSERT INTO analytics (metric_name, value) VALUES (?, ?)", (name, value))
        await self.conn.commit()

    async def get_stats(self) -> dict:
        cursor = await self.conn.execute("SELECT COUNT(*) FROM history WHERE sent_at > datetime('now', '-1 day')")
        daily_posts = (await cursor.fetchone())[0]
        
        cursor = await self.conn.execute("SELECT COUNT(*) FROM history")
        total_posts = (await cursor.fetchone())[0]

        return {"daily": daily_posts, "total": total_posts, "mode": await self.get_mode()}

db = Database()

# ==========================================
# INTELLIGENT SCRAPING ENGINE
# ==========================================
async def scrape_site(session: aiohttp.ClientSession, query: str = None) -> List[Dict]:
    """
    Advanced scraper with retry logic and smart parsing.
    """
    url = BASE_URL + (f"?s={query}" if query else "")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }

    videos = []
    
    for attempt in range(RETRY_ATTEMPTS):
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    raise Exception(f"Status {resp.status}")
                
                html = await resp.text()
                soup = BeautifulSoup(html, 'html.parser')
                
                # Smart Selection: Look for standard post containers
                # Assuming structure: <a> <img /> </a>
                links = soup.find_all('a', href=True)
                
                for link in links:
                    href = link['href']
                    parsed = urlparse(urljoin(BASE_URL, href))
                    path = parsed.path

                    # Skip garbage links
                    skip_patterns = ['/login', '/register', '/category', '/tag', '/page', '.css', '.js']
                    if any(x in path for x in skip_patterns):
                        continue
                    if not path or path == '/': continue

                    # Extract Media
                    img = link.find('img')
                    thumb = urljoin(BASE_URL, img['src']) if img and img.get('src') else None
                    
                    # Filter out layout images
                    if thumb and any(x in thumb for x in ['icon', 'logo', 'pixel', 'blank']): 
                        thumb = None

                    title = (img.get('alt') or link.get_text(strip=True)) or "Video"
                    
                    # Intelligent Ranking (Simple Relevance Score)
                    score = 1.0
                    if query:
                        query_words = set(query.lower().split())
                        title_words = set(title.lower().split())
                        matches = query_words.intersection(title_words)
                        # Boost score if words match
                        score = 1.0 + (len(matches) * 2)
                    
                    videos.append({
                        'id': path.strip('/'),
                        'url': parsed.geturl(),
                        'thumb': thumb,
                        'title': title,
                        'score': score
                    })
                
                # Sort by score (descending)
                videos.sort(key=lambda x: x['score'], reverse=True)
                return videos

        except Exception as e:
            logging.warning(f"Scrape attempt {attempt+1} failed: {e}")
            await asyncio.sleep(2) # Wait before retry
            
    return []

# ==========================================
# FSM & KEYBOARDS
# ==========================================
class BotStates(StatesGroup):
    unverified = State()
    main_menu = State()
    manual_search = State()

def get_main_menu_kb():
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🟢 Auto Mode"), types.KeyboardButton(text="🟡 Manual Mode")],
            [types.KeyboardButton(text="🔴 Stop"), types.KeyboardButton(text="📊 Dashboard")],
            [types.KeyboardButton(text="🔍 Manual Search")]
        ], resize_keyboard=True
    )

def get_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Delete", callback_data="del")]
    ])

# ==========================================
# BOT HANDLERS
# ==========================================
app = FastAPI()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler()
semaphore = asyncio.Semaphore(MAX_CONCURRENT_SENDS)

@app.get("/")
async def root():
    return {"status": "online", "db": "connected"}

@dp.message(CommandStart())
async def start_cmd(msg: types.Message, state: FSMContext):
    if not await db.is_verified(msg.from_user.id):
        await state.set_state(BotStates.unverified)
        await msg.answer("🔒 <b>Access Restricted</b>\nPlease enter the Admin Keyword to proceed.")
        return

    await state.set_state(BotStates.main_menu)
    await show_dashboard(msg, state)

async def show_dashboard(msg: types.Message, state: FSMContext):
    stats = await db.get_stats()
    text = (
        f"🤖 <b>Control Panel</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Status:</b> {stats['mode'].upper()}\n"
        f"📤 <b>Today's Posts:</b> {stats['daily']}\n"
        f"📦 <b>Total DB Size:</b> {stats['total']}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    await msg.answer(text, reply_markup=get_main_menu_kb())

# Global Text Handler
@dp.message()
async def handle_text(msg: types.Message, state: FSMContext):
    current_state = await state.get_state()
    uid = msg.from_user.id
    text = msg.text

    # 1. Verification Handler
    if current_state == BotStates.unverified.state:
        if text == ADMIN_KEYWORD:
            await db.verify_user(uid)
            await state.set_state(BotStates.main_menu)
            await msg.answer("✅ <b>Verified!</b> Welcome to the system.", reply_markup=get_main_menu_kb())
        else:
            await msg.answer("❌ Incorrect Keyword.")
        return

    # 2. Menu Commands
    if current_state == BotStates.main_menu.state:
        if text == "🟢 Auto Mode":
            await db.set_mode("auto")
            await msg.answer("🚀 <b>Auto Mode Activated.</b> Bot is now posting automatically.")
        elif text == "🟡 Manual Mode":
            await db.set_mode("manual")
            await msg.answer("⏸ <b>Manual Mode.</b> Auto-posting paused. Use buttons below.")
        elif text == "🔴 Stop":
            await db.set_mode("off")
            await msg.answer("🛑 <b>System Halted.</b>")
        elif text == "📊 Dashboard":
            await show_dashboard(msg, state)
        elif text == "🔍 Manual Search":
            await state.set_state(BotStates.manual_search)
            await msg.answer("🔍 <b>Enter search term:</b>\n(Send /cancel to go back)")
        return

    # 3. Search Handler
    if current_state == BotStates.manual_search.state:
        if text == "/cancel":
            await state.set_state(BotStates.main_menu)
            await show_dashboard(msg, state)
            return

        # Check if auto is running, warn user
        mode = await db.get_mode()
        if mode == "auto":
            await msg.answer("⚠️ <b>Warning:</b> Auto Mode is ON. Switch to Manual Mode first.")
            return

        await msg.answer(f"🔎 Scanning for: <b>{text}</b>...")
        
        async with aiohttp.ClientSession() as session:
            results = await scrape_site(session, query=text)
            
            if not results:
                await msg.answer("No matching content found.")
            else:
                # Process Sending
                sent_count = await process_batch(results, TARGET_MANUAL)
                await msg.answer(f"✅ <b>Task Complete.</b>\nSent {sent_count} videos.")

# ==========================================
# CORE PROCESSING LOGIC
# ==========================================
async def process_batch(videos: List[Dict], target_chat: int) -> int:
    """
    Sends a batch of videos safely using semaphore to avoid Telegram limits.
    """
    tasks = []
    count = 0
    
    for vid in videos:
        if await db.is_sent(vid['id']):
            continue
        
        await db.save_post(vid['id'], vid['title']) # Save before sending to avoid dupes
        tasks.append(send_with_limit(target_chat, vid))
        count += 1

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    
    return count

async def send_with_limit(chat_id: int, video: Dict):
    """
    Wrapper that enforces semaphore (Anti-Flood).
    """
    async with semaphore:
        try:
            if video['thumb']:
                await bot.send_photo(
                    chat_id=chat_id, 
                    photo=video['thumb'], 
                    caption=f"<a href='{video['url']}'>{video['title']}</a>",
                    reply_markup=get_inline_kb()
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"📹 <a href='{video['url']}'>{video['title']}</a>",
                    reply_markup=get_inline_kb()
                )
        except Exception as e:
            logging.error(f"Error sending {video['id']}: {e}")

@dp.callback_query(F.data == "del")
async def delete_handler(cb: types.CallbackQuery):
    try:
        await cb.message.delete()
    except:
        pass
    await cb.answer()

# ==========================================
# SCHEDULER & BACKGROUND
# ==========================================
async def auto_job():
    mode = await db.get_mode()
    if mode != "auto":
        return

    logging.info("🔄 Auto Job Started")
    async with aiohttp.ClientSession() as session:
        videos = await scrape_site(session)
        if videos:
            count = await process_batch(videos, TARGET_AUTO)
            logging.info(f"Auto Job Finished: {count} posts")

async def cleanup_db():
    """Runs weekly to clear old history"""
    await db.conn.execute("DELETE FROM history WHERE sent_at < datetime('now', '-14 days')")
    await db.conn.commit()
    logging.info("Database cleaned.")

@app.on_event("startup")
async def startup():
    logging.basicConfig(level=logging.INFO)
    await db.connect()
    
    # Schedule
    scheduler.add_job(auto_job, 'interval', minutes=5)
    scheduler.add_job(cleanup_db, 'interval', days=1) # Daily check for cleanup
    scheduler.start()
    
    # Start Bot
    asyncio.create_task(dp.start_polling(bot))
    logging.info("🚀 Professional Bot Started")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
