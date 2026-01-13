import asyncio
import aiosqlite
import logging
import os

# Telegram
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Web Server
from fastapi import FastAPI
import uvicorn

# Scraping
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

# Lock for DB operations to prevent race conditions on Mode setting
db_lock = asyncio.Lock()

# ==========================================
# DATABASE LAYER
# ==========================================
DB_NAME = "bot_data.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Existing tables
        await db.execute('''CREATE TABLE IF NOT EXISTS sent_videos (video_id_or_url TEXT PRIMARY KEY, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS verified_users (user_id INTEGER PRIMARY KEY, verified BOOLEAN DEFAULT 0)''')
        
        # NEW: Global Settings Table
        await db.execute('''CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)''')
        
        # Initialize Mode to 'off' if not set
        await db.execute('''INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('mode', 'off')''')
        
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

# ==========================================
# CORE MODE LOGIC (THREAD-SAFE)
# ==========================================

async def get_current_mode() -> str:
    """Reads the global mode from DB. Returns 'auto', 'manual', or 'off'."""
    async with db_lock:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT value FROM bot_settings WHERE key = 'mode'") as cursor:
                result = await cursor.fetchone()
                return result[0] if result else 'off'

async def set_mode(new_mode: str):
    """Sets the global mode. Must be 'auto', 'manual', or 'off'."""
    async with db_lock:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE bot_settings SET value = ? WHERE key = 'mode'", (new_mode,))
            await db.commit()
            logging.info(f"Mode changed to: {new_mode}")

# ==========================================
# SENT VIDEOS LOGIC
# ==========================================
async def is_video_sent(video_id: str) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM sent_videos WHERE video_id_or_url = ?", (video_id,)) as cursor:
            result = await cursor.fetchone()
            return result is not None

async def save_sent_video(video_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
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

# ==========================================
# KEYBOARDS
# ==========================================

def get_status_keyboard(current_mode: str):
    """Returns inline buttons based on current state."""
    buttons = []
    
    # Auto Mode Button
    if current_mode == 'auto':
        buttons.append([InlineKeyboardButton(text="✅ Auto ON", callback_data="mode_auto")])
    else:
        buttons.append([InlineKeyboardButton(text="Auto ON", callback_data="mode_auto")])

    # Manual Mode Button
    if current_mode == 'manual':
        buttons.append([InlineKeyboardButton(text="✅ Manual ON", callback_data="mode_manual")])
    else:
        buttons.append([InlineKeyboardButton(text="Manual ON", callback_data="mode_manual")])

    # Off Button
    if current_mode == 'off':
        buttons.append([InlineKeyboardButton(text="✅ System OFF", callback_data="mode_off")])
    else:
        buttons.append([InlineKeyboardButton(text="System OFF", callback_data="mode_off")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_inline_keyboard_delete():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Delete ❌", callback_data="delete_msg")]
    ])

# ==========================================
# HANDLERS
# ==========================================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    if not await is_user_verified(user_id):
        await message.answer("Welcome! Please enter the access keyword to proceed.")
    else:
        current_mode = await get_current_mode()
        text = f"System Status: <b>{current_mode.upper()}</b>\n\nUse buttons to change mode."
        await message.answer(text, parse_mode="HTML", reply_markup=get_status_keyboard(current_mode))

@dp.callback_query(F.data.startswith("mode_"))
async def mode_button_handler(callback: types.CallbackQuery):
    """Handles mode switching buttons."""
    action = callback.data.split("_")[1] # auto, manual, off
    
    # Logic to determine new mode based on button clicked
    new_mode = "off"
    if action == "auto":
        new_mode = "auto"
    elif action == "manual":
        new_mode = "manual"
    
    await set_mode(new_mode)
    
    # Update button visual state
    await callback.message.edit_text(
        f"System Status: <b>{new_mode.upper()}</b>\n\nMode changed successfully.",
        parse_mode="HTML",
        reply_markup=get_status_keyboard(new_mode)
    )
    await callback.answer()

@dp.message()
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    text = message.text

    # 1. Authentication Check
    if not await is_user_verified(user_id):
        if text.strip() == "Fxuniverse":
            await verify_user(user_id)
            current_mode = await get_current_mode()
            await message.answer("✅ Access Granted. Select a mode:", reply_markup=get_status_keyboard(current_mode))
        else:
            await message.answer("❌ Incorrect keyword. Access denied.")
        return

    # 2. Manual Search Logic Check
    # RULE: Only search if mode == manual.
    # RULE: Searching does NOT change mode.
    
    current_mode = await get_current_mode()

    if current_mode != "manual":
        # If user types while in Auto or Off mode
        await message.answer("Manual mode is OFF. Use buttons to switch modes.")
        return

    # If we are here, mode is MANUAL. Proceed with search.
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
                            text=f"📹 {video['url']}",
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
    try:
        await callback.message.delete()
        await callback.answer()
    except Exception:
        pass

# ==========================================
# SCRAPING LOGIC
# ==========================================

async def scrape_site(session: aiohttp.ClientSession, search_query: str = None):
    headers = {"User-Agent": "Mozilla/5.0"}
    videos = []
    try:
        async with session.get(BASE_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as configuration:
            if configuration.status == 200:
                html = await configuration.text()
                soup = BeautifulSoup(html, 'html.parser')
                
                # Heuristic: Find all links that contain images (likely video thumbnails)
                for link in soup.find_all('a', href=True):
                    href = link['href']
                    
                    # Check if link is internal
                    full_url = urljoin(BASE_URL, href)
                    
                    # Extract ID to prevent duplicates
                    parsed = urlparse(full_url)
                    video_id = parsed.path 
                    if not video_id or video_id == '/': continue

                    # Find Thumbnail
                    img = link.find('img')
                    thumbnail = urljoin(BASE_URL, img['src']) if img and img.get('src') else None
                    
                    # Filter Search Query
                    text = link.get_text().lower()
                    if search_query and search_query.lower() not in text:
                        continue

                    videos.append({'id': video_id, 'url': full_url, 'thumbnail': thumbnail})
    except Exception as e:
        logging.error(f"Scrape Error: {e}")
    
    return videos

async def auto_scrape_task():
    """
    AUTO SCRAPER RULE:
    - Scheduler always runs.
    - But it must POST only if mode == auto.
    - If mode != auto -> scraper returns immediately (no post).
    """
    current_mode = await get_current_mode()
    
    if current_mode != "auto":
        # Mode is 'manual' or 'off'. Do nothing.
        logging.info(f"Auto-scrape skipped (Mode is {current_mode}).")
        return
    
    logging.info("Auto-scrape running...")
    
    async with aiohttp.ClientSession() as session:
        videos = await scrape_site(session)
        for video in videos:
            if await is_video_sent(video['id']):
                continue
            try:
                if video['thumbnail']:
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
    # Start bot
    asyncio.create_task(dp.start_polling(bot))
    # Start scheduler (every 5 minutes)
    scheduler.add_job(auto_scrape_task, 'interval', minutes=5)
    scheduler.start()
    logging.info("Bot started.")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
