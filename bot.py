import asyncio
import aiosqlite
import logging
import os
import re
from datetime import datetime
from typing import List, Dict, Optional

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
# CONFIGURATION
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_IDS_STR = os.getenv("GROUP_IDS", "-1001234567890,-1009876543210")
ADMIN_KEYWORD = os.getenv("ADMIN_KEYWORD", "Fxuniverse")

try:
    GROUP_IDS = [int(gid.strip()) for gid in GROUP_IDS_STR.split(',')]
except ValueError:
    logging.error("Invalid GROUP_IDS. Using defaults.")
    GROUP_IDS = [-1001234567890, -1009876543210]

TARGET_AUTO = GROUP_IDS[0]
TARGET_MANUAL = GROUP_IDS[1]
BASE_URL = "https://lolpol2.com/"

# ==========================================
# DATABASE LAYER
# ==========================================
DB_NAME = "bot_final.db"

class Database:
    def __init__(self):
        self.db_path = DB_NAME

    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self._init_tables()

    async def _init_tables(self):
        await self.conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, is_verified BOOLEAN DEFAULT 0)''')
        await self.conn.execute('''CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)''')
        await self.conn.execute('''CREATE TABLE IF NOT EXISTS history (post_id TEXT PRIMARY KEY, sent_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
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
        # Only allow valid modes
        if mode not in ['off', 'auto', 'manual']:
            return
        await self.conn.execute('INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)', ('mode', mode))
        await self.conn.commit()

    async def is_sent(self, post_id: str) -> bool:
        cursor = await self.conn.execute("SELECT 1 FROM history WHERE post_id = ?", (post_id,))
        return await cursor.fetchone() is not None

    async def save_post(self, post_id: str):
        try:
            await self.conn.execute("INSERT INTO history (post_id) VALUES (?)", (post_id,))
            await self.conn.commit()
        except aiosqlite.IntegrityError:
            pass

db = Database()

# ==========================================
# INTELLIGENT SCRAPING ENGINE
# ==========================================
def clean_text(text: str) -> str:
    """Removes special chars and lowers text for fuzzy matching."""
    if not text: return ""
    return re.sub(r'[^a-zA-Z0-9\s]', '', text).lower().strip()

async def scrape_site(session: aiohttp.ClientSession, query: str = None) -> List[Dict]:
    """
    Advanced scraper with partial matching logic.
    """
    url = BASE_URL + (f"?s={query}" if query else "")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }

    videos = []
    
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                logging.error(f"Scrape status: {resp.status}")
                return []
            
            html = await resp.text()
            soup = BeautifulSoup(html, 'html.parser')
            
            # Find all links
            links = soup.find_all('a', href=True)
            # Filter duplicates by URL to prevent sending same link twice
            seen_urls = set()
            
            for link in links:
                href = link['href']
                
                # Normalize URL
                full_url = urljoin(BASE_URL, href)
                parsed = urlparse(full_url)
                path = parsed.path

                # 1. Skip Garbage
                skip_patterns = ['/login', '/register', '/category', '/tag', '/page', '.css', '.js', 'wp-content', '/feed', '/author']
                if any(x in path for x in skip_patterns):
                    continue
                if not path or path == '/': continue
                
                # URL De-duplication
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                # 2. Safe Extraction
                try:
                    img = link.find('img')
                    thumb = None
                    if img:
                        # Try standard src, then data-src (lazy loading)
                        thumb = img.get('src') or img.get('data-src')
                        if thumb:
                            thumb = urljoin(BASE_URL, thumb)
                            # Skip tiny images/icons
                            if 'icon' in thumb.lower() or thumb.endswith('.gif'):
                                thumb = None
                    
                    # Extract Title (Alt text or Link Text)
                    title = "Video Post"
                    if img:
                        title = img.get('alt') or title
                    if title == "Video Post":
                        # Get text from inside the link
                        raw_text = link.get_text(separator=' ', strip=True)
                        if raw_text:
                            title = raw_text[:60] # Truncate long titles

                    # 3. Intelligent Search Filter
                    # If searching, we check if the query is PART of the title (not just exact)
                    match_score = 0
                    if query:
                        query_clean = clean_text(query)
                        title_clean = clean_text(title)
                        url_clean = clean_text(full_url)
                        
                        # Check if query is inside title OR url
                        if query_clean in title_clean or query_clean in url_clean:
                            match_score = 1
                        else:
                            # If no match, skip this video
                            continue
                    
                    video_id = path.strip('/').replace('/', '_')
                    
                    videos.append({
                        'id': video_id,
                        'url': full_url,
                        'thumb': thumb,
                        'title': title
                    })
                except Exception:
                    continue

            logging.info(f"Scraped {len(videos)} valid videos.")

    except Exception as e:
        logging.error(f"Scrape Critical Error: {e}")

    return videos

# ==========================================
# BOT CONTROLS
# ==========================================
class BotStates(StatesGroup):
    unverified = State()
    main_menu = State()
    manual_search = State()

def get_main_menu_kb():
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🟢 Auto ON"), types.KeyboardButton(text="🟡 Manual ON")],
            [types.KeyboardButton(text="🔴 STOP / OFF"), types.KeyboardButton(text="🔍 Search")]
        ], resize_keyboard=True
    )

def get_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Delete", callback_data="del")]
    ])

app = FastAPI()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler()

@app.get("/")
async def root():
    return {"status": "ok"}

@dp.message(CommandStart())
async def start_cmd(msg: types.Message, state: FSMContext):
    if not await db.is_verified(msg.from_user.id):
        await state.set_state(BotStates.unverified)
        await msg.answer("🔒 Enter Admin Keyword:")
        return

    await state.set_state(BotStates.main_menu)
    current_mode = await db.get_mode()
    await msg.answer(f"Welcome. Current Mode: {current_mode.upper()}", reply_markup=get_main_menu_kb())

@dp.message()
async def handle_text(msg: types.Message, state: FSMContext):
    uid = msg.from_user.id
    text = msg.text
    current_state = await state.get_state()

    # --- VERIFICATION ---
    if current_state == BotStates.unverified.state:
        if text == ADMIN_KEYWORD:
            await db.verify_user(uid)
            await state.set_state(BotStates.main_menu)
            await msg.answer("✅ Verified.", reply_markup=get_main_menu_kb())
        else:
            await msg.answer("❌ Wrong Keyword.")
        return

    # --- MODE CONTROLS ---
    if current_state == BotStates.main_menu.state:
        if text == "🟢 Auto ON":
            await db.set_mode("auto")
            await msg.answer("🚀 <b>Auto Mode ACTIVE.</b>\nBot will post automatically.", parse_mode=ParseMode.HTML)
        elif text == "🟡 Manual ON":
            await db.set_mode("manual")
            await msg.answer("⏸ <b>Manual Mode ACTIVE.</b>\nAuto-posting paused. Use Search.", parse_mode=ParseMode.HTML)
        elif text == "🔴 STOP / OFF":
            await db.set_mode("off")
            await msg.answer("🛑 <b>STOPPED.</b>\nAll activity halted.", parse_mode=ParseMode.HTML)
        elif text == "🔍 Search":
            await state.set_state(BotStates.manual_search)
            await msg.answer("🔎 Enter keyword to search:")
        return

    # --- SEARCH LOGIC ---
    if current_state == BotStates.manual_search.state:
        # Safety: Check if Auto is running. Warn user if so.
        mode = await db.get_mode()
        if mode == "auto":
            await msg.answer("⚠️ Warning: Auto Mode is ON. Please turn it OFF first if you want to do manual searching to avoid confusion.")
        
        await msg.answer(f"🔎 Searching for: <b>{text}</b>...", parse_mode=ParseMode.HTML)
        
        async with aiohttp.ClientSession() as session:
            results = await scrape_site(session, query=text)
            
            if not results:
                await msg.answer("😔 No results found. Try a shorter keyword.")
            else:
                sent = 0
                for vid in results:
                    if await db.is_sent(vid['id']): continue
                    
                    await db.save_post(vid['id'])
                    
                    try:
                        if vid['thumb']:
                            await bot.send_photo(
                                chat_id=TARGET_MANUAL, 
                                photo=vid['thumb'], 
                                caption=f"<a href='{vid['url']}'>{vid['title']}</a>",
                                reply_markup=get_inline_kb()
                            )
                        else:
                            await bot.send_message(
                                chat_id=TARGET_MANUAL,
                                text=f"🎬 <a href='{vid['url']}'>{vid['title']}</a>",
                                reply_markup=get_inline_kb()
                            )
                        sent += 1
                        await asyncio.sleep(1) 
                    except Exception as e:
                        logging.error(f"Send error: {e}")
                
                await msg.answer(f"✅ Sent <b>{sent}</b> videos to group.", parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "del")
async def delete_handler(cb: types.CallbackQuery):
    try:
        await cb.message.delete()
    except:
        pass
    await cb.answer()

# ==========================================
# BACKGROUND JOB
# ==========================================
async def auto_job():
    mode = await db.get_mode()
    # CRITICAL: Stop immediately if mode is not auto
    if mode != "auto":
        return

    logging.info("Running Auto Job...")
    async with aiohttp.ClientSession() as session:
        videos = await scrape_site(session)
        if videos:
            sent = 0
            for vid in videos:
                if await db.is_sent(vid['id']): continue
                await db.save_post(vid['id'])
                
                try:
                    if vid['thumb']:
                        await bot.send_photo(TARGET_AUTO, photo=vid['thumb'], caption=f"<a href='{vid['url']}'>{vid['title']}</a>", reply_markup=get_inline_kb())
                    else:
                        await bot.send_message(TARGET_AUTO, text=f"🎬 <a href='{vid['url']}'>{vid['title']}</a>", reply_markup=get_inline_kb())
                    sent += 1
                    await asyncio.sleep(1)
                except Exception as e:
                    logging.error(f"Auto send error: {e}")
            
            logging.info(f"Auto sent {sent} videos.")

@app.on_event("startup")
async def startup():
    logging.basicConfig(level=logging.INFO)
    await db.connect()
    
    # Set default mode if empty
    if await db.get_mode() not in ['off', 'auto', 'manual']:
        await db.set_mode("off")

    scheduler.add_job(auto_job, 'interval', minutes=5)
    scheduler.start()
    
    # CRITICAL: drop_pending_updates prevents the bot from fighting itself
    asyncio.create_task(dp.start_polling(bot, drop_pending_updates=True))
    logging.info("🚀 Bot Started (Final Version)")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
