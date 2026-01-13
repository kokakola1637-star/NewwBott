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
DB_NAME = "bot_fixed_stop.db"

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
# SCRAPING ENGINE
# ==========================================
def clean_text(text: str) -> str:
    if not text: return ""
    return re.sub(r'[^a-zA-Z0-9\s]', '', text).lower().strip()

async def scrape_site(session: aiohttp.ClientSession, query: str = None) -> List[Dict]:
    url = BASE_URL + (f"?s={query}" if query else "")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }
    videos = []
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200: return []
            html = await resp.text()
            soup = BeautifulSoup(html, 'html.parser')
            links = soup.find_all('a', href=True)
            seen_urls = set()
            
            for link in links:
                href = link['href']
                full_url = urljoin(BASE_URL, href)
                parsed = urlparse(full_url)
                path = parsed.path

                # Skip garbage
                if any(x in path for x in ['/login', '/register', '/category', '/tag', '/page', '.css', '.js', 'wp-content']):
                    continue
                if not path or path == '/': continue
                if full_url in seen_urls: continue
                seen_urls.add(full_url)

                # Extract Data
                try:
                    img = link.find('img')
                    thumb = None
                    if img:
                        thumb = img.get('src') or img.get('data-src')
                        if thumb: thumb = urljoin(BASE_URL, thumb)
                        if thumb and ('icon' in thumb.lower() or thumb.endswith('.gif')): thumb = None

                    title = "Video Post"
                    if img: title = img.get('alt') or title
                    if title == "Video Post": title = link.get_text(separator=' ', strip=True)[:60]

                    # Search Filter
                    if query:
                        query_clean = clean_text(query)
                        title_clean = clean_text(title)
                        url_clean = clean_text(full_url)
                        if not (query_clean in title_clean or query_clean in url_clean):
                            continue

                    video_id = path.strip('/').replace('/', '_')
                    videos.append({'id': video_id, 'url': full_url, 'thumb': thumb, 'title': title})
                except:
                    continue
            logging.info(f"Scraped {len(videos)} videos.")
    except Exception as e:
        logging.error(f"Scrape Error: {e}")
    return videos

# ==========================================
# BOT STATES & HANDLERS
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
    
    # If already verified, ensure we are in main menu state
    await state.set_state(BotStates.main_menu)
    await msg.answer("Welcome. Ready.", reply_markup=get_main_menu_kb())

@dp.message()
async def handle_text(msg: types.Message, state: FSMContext):
    current_state = await state.get_state()
    text = msg.text

    # 1. Verification Logic (Only active if in 'unverified' state)
    if current_state == BotStates.unverified.state:
        if text == ADMIN_KEYWORD:
            await db.verify_user(msg.from_user.id)
            await state.set_state(BotStates.main_menu)
            # FIX: Show keyboard again after verification
            await msg.answer("✅ Verified.", reply_markup=get_main_menu_kb())
        else:
            await msg.answer("❌ Wrong Keyword.")
        return

    # 2. Global STOP Logic
    # This acts as a 'kill switch' regardless of which sub-state you are in (Menu, Search, etc)
    if text == "🔴 STOP / OFF":
        await db.set_mode("off")
        # Force state back to main_menu
        await state.set_state(BotStates.main_menu)
        await msg.answer("🛑 STOPPED. All actions cancelled immediately.", reply_markup=get_main_menu_kb())
        return

    # 3. Main Menu Controls
    # We only process these if we are currently in the main menu state
    if current_state == BotStates.main_menu.state:
        if text == "🟢 Auto ON":
            await db.set_mode("auto")
            await msg.answer("🚀 Auto Mode ON.")
        elif text == "🟡 Manual ON":
            await db.set_mode("manual")
            await msg.answer("⏸ Manual Mode ON.")
        elif text == "🔍 Search":
            await state.set_state(BotStates.manual_search)
            await msg.answer("🔎 Enter keyword (or click STOP to cancel):")
        return

    # 4. Search Logic
    if current_state == BotStates.manual_search.state:
        # User entered a search term
        await msg.answer(f"🔎 Searching for: {text}...")
        
        # Scrape first to get total count quickly
        async with aiohttp.ClientSession() as session:
            results = await scrape_site(session, query=text)
            
            if not results:
                await msg.answer("No results found.")
                # Return to main menu so buttons are available
                await state.set_state(BotStates.main_menu)
            else:
                sent_count = 0
                for vid in results:
                    # CRITICAL CHECK: State check inside the loop
                    # If user clicked STOP, state is now 'main_menu', loop breaks
                    loop_state = await state.get_state()
                    if loop_state != BotStates.manual_search.state:
                        logging.info("Search aborted by user (State changed).")
                        await msg.answer("⚠️ Search Aborted.")
                        return 

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
                        sent_count += 1
                        await asyncio.sleep(1) 
                    except Exception as e:
                        logging.error(f"Send error: {e}")
                
                if sent_count > 0:
                    await msg.answer(f"✅ Sent {sent_count} videos.")
                
                # Done searching, return to main menu to restore buttons
                await state.set_state(BotStates.main_menu)
        return

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
    # CRITICAL: If mode is OFF, do nothing immediately
    if mode != "auto":
        return

    logging.info("Running Auto Job...")
    async with aiohttp.ClientSession() as session:
        videos = await scrape_site(session)
        if videos:
            sent = 0
            for vid in videos:
                # Double check mode inside the loop in case it changed
                mode = await db.get_mode()
                if mode != "auto":
                    logging.info("Auto job aborted mid-cycle.")
                    break

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
    
    # Ensure valid mode on startup
    if await db.get_mode() not in ['off', 'auto', 'manual']:
        await db.set_mode("off")

    scheduler.add_job(auto_job, 'interval', minutes=5)
    scheduler.start()
    
    # Start bot polling
    asyncio.create_task(dp.start_polling(bot, drop_pending_updates=True))
    logging.info("🚀 Bot Started (Stop Button Fixed)")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
