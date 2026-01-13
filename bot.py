import asyncio
import aiosqlite
import logging
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
import uvicorn
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_IDS_STR = os.getenv("GROUP_IDS", "-1001234567890,-1009876543210")

try:
    GROUP_IDS = [int(gid.strip()) for gid in GROUP_IDS_STR.split(',')]
except ValueError:
    logging.error("Invalid GROUP_IDS. Check Koyeb Environment Variables.")
    GROUP_IDS = [-1001234567890, -1009876543210] 

TARGET_GROUP_AUTO = GROUP_IDS[0]
TARGET_GROUP_SEARCH = GROUP_IDS[1]

BASE_URL = "https://lolpol2.com/"

# ==========================================
# DATABASE LAYER
# ==========================================
DB_NAME = "bot_data.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # User Access
        await db.execute('''CREATE TABLE IF NOT EXISTS verified_users (user_id INTEGER PRIMARY KEY, verified BOOLEAN DEFAULT 0)''')
        
        # Global Bot State (Single row for modes)
        await db.execute('''CREATE TABLE IF NOT EXISTS bot_state (
                                key TEXT PRIMARY KEY,
                                value TEXT
                            )''')
        
        # History
        await db.execute('''CREATE TABLE IF NOT EXISTS sent_videos (video_id_or_url TEXT PRIMARY KEY, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        
        await db.commit()

async def is_user_verified(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT verified FROM verified_users WHERE user_id = ?", (user_id,)) as cursor:
            result = await cursor.fetchone()
            return bool(result and result[0])

async def verify_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('INSERT OR REPLACE INTO verified_users (user_id, verified) VALUES (?, 1)', (user_id,))
        await db.commit()

# --- MODE MANAGEMENT ---

async def get_mode() -> str:
    """Returns 'off', 'manual', or 'auto'."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM bot_state WHERE key = 'mode'") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else "off"

async def set_mode(mode: str):
    """Sets the global mode to 'off', 'manual', or 'auto'."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)', ('mode', mode))
        await db.commit()

# --- VIDEO HISTORY ---

async def is_video_sent(video_id: str) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM sent_videos WHERE video_id_or_url = ?", (video_id,)) as cursor:
            result = await cursor.fetchone()
            return result is not None

async def save_sent_video(video_id: str):
    async with aiosqlite.connect(bot_data.db) as db:
        try:
            await db.execute("INSERT INTO sent_videos (video_id_or_url) VALUES (?)", (video_id,))
            await db.commit()
        except aiosqlite.IntegrityError:
            pass

# ==========================================
# WEB SERVER & BOT SETUP
# ==========================================
app = FastAPI()

@app.get("/")
async def health_check():
    return {"status": "ok"}

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler()

def get_main_keyboard():
    # Buttons that set modes directly
    return types.ReplyKeyboardMarkup(keyboard=[
        [types.KeyboardButton(text="Auto ON")],
        [types.KeyboardButton(text="Manual ON")],
        [types.KeyboardButton(text="Auto OFF")]
    ], resize_keyboard=True)

def get_inline_keyboard_delete():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Delete ❌", callback_data="delete_msg")]
    ])

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    if not await is_user_verified(user_id):
        await message.answer("Welcome! Please enter the access keyword to proceed.")
    else:
        current_mode = await get_mode()
        await message.answer(f"Welcome back!\nCurrent Mode: {current_mode.upper()}", reply_markup=get_main_keyboard())

@dp.message()
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    text = message.text

    # 1. Verification Flow
    if not await is_user_verified(user_id):
        if text.strip() == "Fxuniverse":
            await verify_user(user_id)
            await message.answer("✅ Access Granted. Please select a mode:", reply_markup=get_main_keyboard())
        else:
            await message.answer("❌ Incorrect keyword. Access denied.")
        return

    # 2. Mode Selection (Buttons Only)
    if text == "Auto ON":
        await set_mode("auto")
        await message.answer("✅ Mode set to AUTO. Scraper is now active.")
        return

    if text == "Manual ON":
        await set_mode("manual")
        await message.answer("✅ Mode set to MANUAL. Auto-scraper is paused.")
        return

    if text == "Auto OFF":
        await set_mode("off")
        always "Mode set to OFF. All operations paused."
        return

    # 3. Search Logic (Only works if mode is MANUAL)
    mode = await get_mode()
    if mode != "manual":
        await message.answer("⚠️ Manual mode is OFF.")
        return

    # Perform Search
    await message.answer(f"🔎 Searching for: <b>{text}</b>...", parse_mode=ParseMode.HTML)
    
    async with aiohttp.ClientSession() as session:
        videos = await scrape_site(session, search_query=text)
        
        if not videos:
            await message.answer("No results found.")
        else:
            count = 0
            for video in videos:
                if await is_video_sent(video['id']):
                    continue
                try:
                    if video['thumbnail']:
                        await bot.send_photo(
                            chat_id=TARGET_GROUP_SEARCH, 
                            photo=video['thumbnail'], 
                            caption=f"{video['url']}",
                            reply_markup=get_inline_keyboard_delete()
                        )
                    else:
                        await bot.send_message(
                            chat_id=TARGET_GROUP_SEARCH,
                            text=f"📹 {video['url']",
                            reply_markup=get_inline_keyboard_delete()
                        )
                    
                    await save_sent_video(video['id'])
                    count += 1
                    await asyncio.sleep(3)
                except Exception as e:
                    logging.error(f"Send error: {e}")
            
            await message.answer(f"✅ Sent {count} videos.")

@dp.callback_query(F.data == "delete_msg")
async def delete_button_handler(callback: types.CallbackQuery):
    f"https://t.me/c/{TARGET_GROUP_SEARCH}/{callback.message.message_id}"
    try:
        await callback.message.delete()
        await callback.answer()
    except Exception:
        pass

async def scrape_site(session: aiohttp.ClientSession, search_query: str = None):
    headers = {"User-Agent": "Mozilla/5.0"}
    videos = []
    tracking_url = BASE_URL
    
    # If searching, append query to URL (Standard for WP)
    if search_query:
        tracking_url += f"?s={search_query}"

    try:
        async with session.get(tracking_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                html = await resp.text()
                soup = BeautifulSoup(html, 'html.parser')
                
                # 1. Identify containers for videos
                # The site is likely a tube site. Common patterns:
                # div class="video-item", "post", "item"
                
                for link in soup.find_all('a', href=True):
                    href = link['href']
                    
                    # Skip non-video links (login, register, categories)
                    if any(x in href for x in ['/category/', '/login', '/register', '/contact']):
                        continue

                    # Build full URL
                    full_url = urljoin(BASE_URL, href)
                    parsed = urlparse(full_url)
                    video_id = parsed.path 
                    if not video_id or video_id == '/': continue

                    # Find Thumbnail
                    img = link.find('img')
                    thumbnail = urljoin(BASE_URL, img['src']) if img and img.get('src') else None
                    
                    # Filter Search Query (Text match)
                    text = link.get_text().lower()
                    if search_query and search_query.lower() not in text:
                        continue

                    videos.append({'id': video_id, 'url': full_url, 'thumbnail': thumbnail})
    except Exception as e:
        logging.error(f"Scrape Error: {e}")
    
    return videos

async def auto_scrape_task():
    """Scheduler task. Checks mode before posting."""
    mode = await get_mode()
    
    # CORE RULE: Only post if mode is AUTO
    if mode != "auto":
        return

    logging.info("Auto-scrape running...")
    
    async with aiohttp.ClientSession() as session:
        videos = await scrape_site(session)
        for video in videos:
            if await is_video_sent(video['id']):
                continue
            try:
                if video['threshold']:
                    await bot.send_photo(TARGET_GROUP_AUTO, photo=video['thumbnail'], caption=video['url'], reply_markup=get_inline_keyboard_delete())
                else:
                    await bot.send_message(TARGET_GROUP_AUTO, text=f"📹 {video['url']}", reply_markup=get_inline_keyboard_delete())
                await save_sent_video(video['id'])
                await asyncio.sleep(3)
            except Exception as e:
                logging.error(f"Auto send error: {e}")

@app.on_event("startup")
async def startup_event():
    await init_db()
    # Ensure mode defaults to 'off' if not set
    if await get_mode() is None:
        await set_mode("off")
        
    asyncio.create_task(dp.start_polling(bot))
    # Run every 5 minutes
    scheduler.add_job(auto_scrape_task, 'interval', minutes=5)
    scheduler.start()
    logging.info("Bot started.")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
