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

# Parse Group IDs safely
try:
    GROUP_IDS = [int(gid.strip()) for gid in GROUP_IDS_STR.split(',')]
except ValueError:
    logging.error("Invalid GROUP_IDS format. Falling back to defaults.")
    GROUP_IDS = [-1001234567890, -1009876543210]

TARGET_GROUP_AUTO = GROUP_IDS[0]
TARGET_GROUP_SEARCH = GROUP_IDS[1]

BASE_URL = "https://lolpol2.com/"
ADMIN_ACCESS_KEYWORD = "Fxuniverse"  # Keyword to verify users

# ==========================================
# DATABASE LAYER (Optimized)
# ==========================================
DB_NAME = "bot_data.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Verified Users
        await db.execute('''CREATE TABLE IF NOT EXISTS verified_users 
                            (user_id INTEGER PRIMARY KEY, verified BOOLEAN DEFAULT 1, joined_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        
        # Bot State (Mode)
        await db.execute('''CREATE TABLE IF NOT EXISTS bot_state 
                            (key TEXT PRIMARY KEY, value TEXT)''')
        
        # Sent History (to prevent duplicates)
        await db.execute('''CREATE TABLE IF NOT EXISTS sent_videos 
                            (video_id TEXT PRIMARY KEY, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        
        await db.commit()

async def is_user_verified(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as bot_db:
        async with bot_db.execute("SELECT verified FROM verified_users WHERE user_id = ?", (user_id,)) as cursor:
            result = await cursor.fetchone()
            return bool(result and result[0])

async def verify_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('INSERT OR REPLACE INTO verified_users (user_id, verified) VALUES (?, 1)', (user_id,))
        await db.commit()

async def get_mode() -> str:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM bot_state WHERE key = 'mode'") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else "off"

async def set_mode(mode: str):
    allowed_modes = ['off', 'manual', 'auto']
    if mode not in allowed_modes:
        raise ValueError(f"Invalid mode: {mode}")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)', ('mode', mode))
        await db.commit()

async def is_video_sent(video_id: str) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM sent_videos WHERE video_id = ?", (video_id,)) as cursor:
            result = await cursor.fetchone()
            return result is not None

async def save_sent_video(video_id: str):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT INTO sent_videos (video_id) VALUES (?)", (video_id,))
            await db.commit()
    except aiosqlite.IntegrityError:
        pass # Already exists

# Maintenance: Keep DB size manageable
async def cleanup_old_records():
    """Delete records older than 7 days."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM sent_videos WHERE timestamp < datetime('now', '-7 days')")
        await db.commit()
        logging.info("Database cleanup completed.")

# ==========================================
# SCRAPING ENGINE (Intelligent)
# ==========================================
VIDEO_EXTENSIONS = ('.mp4', '.webm', '.ogg', '.mov')
EXT_STREAMING_DOMAINS = ('youtube.com', 'youtu.be', 'vimeo.com', 'pornhub.com', 'xvideos.com', 'xnxx.com')

async def scrape_site(session: aiohttp.ClientSession, search_query: str = None) -> List[Dict]:
    """
    Scrapes the target site intelligently.
    Returns a list of video dictionaries: {id, url, thumbnail, title}.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }
    videos = []
    target_url = BASE_URL
    
    if search_query:
        target_url += f"?s={search_query}"

    try:
        async with session.get(target_url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                logging.error(f"Scrape failed with status: {resp.status}")
                return []
            
            html = await resp.text()
            soup = BeautifulSoup(html, 'html.parser')

            # Iterate through potential video containers
            # Adjust this selector based on the actual HTML structure of lolpol2.com
            # We look for <a> tags that contain images or specific classes
            for link in soup.find_all('a', href=True):
                href = link['href']
                
                # 1. Filter: Skip navigation, login, category links
                if any(x in href for x in ['/category/', '/login', '/register', '/contact', '/tag/', '/page/']):
                    continue
                
                # 2. Filter: Ensure it looks like a post/video link
                # Usually posts end with .html or just a slug, excluding extensions like .css, .jpg
                parsed = urlparse(href)
                if parsed.path.endswith(('.css', '.js', '.xml', '.json')): continue
                if not parsed.path or parsed.path == '/': continue

                full_url = urljoin(BASE_URL, href)
                video_id = parsed.path.strip('/')

                # 3. Extract Thumbnail (Find first <img> inside the <a>)
                img_tag = link.find('img')
                thumbnail = None
                if img_tag and img_tag.get('src'):
                    thumbnail = urljoin(BASE_URL, img_tag['src'])
                    # Ignore tiny icons (likely layout elements)
                    if any(x in thumbnail.lower() for x in ['icon', 'logo', 'sprite']):
                        thumbnail = None

                # 4. Extract Title
                # Priority: img alt attribute, or link text
                title = img_tag.get('alt') if img_tag else link.get_text(strip=True)
                if not title: title = "Video"

                # 5. Filter Search Query (Text match)
                if search_query:
                    if search_query.lower() not in title.lower():
                        continue

                videos.append({
                    'id': video_id,
                    'url': full_url,
                    'thumbnail': thumbnail,
                    'title': title
                })

    except asyncio.TimeoutError:
        logging.error("Scraping timed out.")
    except Exception as e:
        logging.error(f"Scraping Exception: {e}")

    return videos

# ==========================================
# BOT LOGIC
# ==========================================
app = FastAPI()

@app.get("/")
async def health_check():
    return {"status": "ok", "mode": await get_mode()}

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Keyboards
def get_main_keyboard():
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Auto ON"), types.KeyboardButton(text="Manual ON")],
            [types.KeyboardButton(text="Auto OFF"), types.KeyboardButton(text="Status")]
        ], 
        resize_keyboard=True
    )

def get_inline_keyboard_delete():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Delete ❌", callback_data="delete_msg")]
    ])

# Handlers
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    if not await is_user_verified(user_id):
        await message.answer("👋 Welcome!\nPlease enter the access keyword to proceed.")
    else:
        await show_main_menu(message)

async def show_main_menu(message: types.Message):
    current_mode = await get_mode()
    status_text = f"🤖 <b>Bot Status</b>\n\n" \
                  f"Current Mode: <u>{current_mode.upper()}</u>\n" \
                  f"Target Group (Auto): <code>{TARGET_GROUP_AUTO}</code>\n" \
                  f"Target Group (Search): <code>{TARGET_GROUP_SEARCH}</code>"
    await message.answer(status_text, reply_markup=get_main_keyboard())

@dp.message()
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    text = message.text

    # 1. Verification Logic
    if not await is_user_verified(user_id):
        if text.strip() == ADMIN_ACCESS_KEYWORD:
            await verify_user(user_id)
            await message.answer("✅ <b>Access Granted!</b>\nYou can now control the bot.", parse_mode=ParseMode.HTML)
            await show_main_menu(message)
        else:
            await message.answer("❌ Incorrect keyword. Access denied.")
        return

    # 2. Command Logic
    if text == "Auto ON":
        await set_mode("auto")
        await message.answer("✅ <b>Mode set to AUTO.</b>\nBot will scrape and post automatically every few minutes.", parse_mode=ParseMode.HTML)
        return

    if text == "Manual ON":
        await set_mode("manual")
        await message.answer("✅ <b>Mode set to MANUAL.</b>\nAuto-scraper is paused. You can search now.", parse_mode=ParseMode.HTML)
        return

    if text == "Auto OFF":
        await set_mode("off")
        await message.answer("🛑 <b>Mode set to OFF.</b>\nAll operations paused.", parse_mode=ParseMode.HTML)
        return

    if text == "Status":
        await show_main_menu(message)
        return

    # 3. Manual Search Logic
    mode = await get_mode()
    if mode == "auto":
        await message.answer("⚠️ Please switch to 'Manual ON' to perform manual searches.")
        return

    if mode == "off":
        await message.answer("⚠️ Bot is OFF. Turn it on first.")
        return

    # If in Manual mode, treat text as a search query
    await message.answer(f"🔎 Searching for: <b>{text}</b>...", parse_mode=ParseMode.HTML)
    
    async with aiohttp.ClientSession() as session:
        results = await scrape_site(session, search_query=text)
        
        if not results:
            await message.answer("🤷‍♂️ No results found for that query.")
        else:
            await process_and_send(message, results, TARGET_GROUP_SEARCH)

# Core Sending Logic (Reusable for Auto and Manual)
async def process_and_send(source_message: Optional[types.Message], videos: List[Dict], chat_id: int):
    """
    Processes a list of videos, filters sent ones, and sends them concurrently.
    """
    count = 0
    tasks = []

    for video in videos:
        if await is_video_sent(video['id']):
            continue
        
        # Save to DB immediately to avoid race conditions in concurrent tasks
        await save_sent_video(video['id'])
        
        # Create a task for sending
        tasks.append(send_single_video(chat_id, video))
        count += 1

    if not tasks:
        if source_message: await source_message.answer("No new videos to send (all already sent).")
        return

    # Run sends concurrently
    await asyncio.gather(*tasks, return_exceptions=True)
    
    if source_message:
        await source_message.answer(f"✅ Successfully sent <b>{count}</b> new videos!", parse_mode=ParseMode.HTML)

async def send_single_video(chat_id: int, video: Dict):
    """Helper to send a single video/photo with error handling."""
    try:
        if video['thumbnail']:
            await bot.send_photo(
                chat_id=chat_id, 
                photo=video['thumbnail'], 
                caption=f"<a href='{video['url']}'>{video['title']}</a>",
                reply_markup=get_inline_keyboard_delete()
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📹 <a href='{video['url']}'>{video['title']}</a>",
                reply_markup=get_inline_keyboard_delete()
            )
    except Exception as e:
        logging.error(f"Failed to send video {video['id']}: {e}")

@dp.callback_query(F.data == "delete_msg")
async def delete_button_handler(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
        await callback.answer()
    except Exception:
        await callback.answer()

# ==========================================
# BACKGROUND TASKS
# ==========================================
async def auto_scrape_task():
    """Runs on scheduler. Checks mode and posts if AUTO."""
    mode = await get_mode()
    
    if mode != "auto":
        return

    logging.info("🔄 Auto-scrape cycle started...")
    
    try:
        async with aiohttp.ClientSession() as session:
            # Get fresh videos
            videos = await scrape_site(session)
            
            # We don't use process_and_send here to avoid replying to a user
            # Instead we directly iterate and send
            new_videos = [v for v in videos if not await is_video_sent(v['id'])]
            
            if not new_videos:
                logging.info("No new videos found in auto cycle.")
                return

            logging.info(f"Found {len(new_videos)} new videos. Posting...")

            # Mark as sent before posting to avoid double posts on restart
            for v in new_videos:
                await save_sent_video(v['id'])

            # Send concurrently
            tasks = [send_single_video(TARGET_GROUP_AUTO, v) for v in new_videos]
            await asyncio.gather(*tasks, return_exceptions=True)
            
            logging.info(f"Auto-scrape finished. Posted {len(new_videos)} videos.")
            
    except Exception as e:
        logging.error(f"Auto-scrape task error: {e}")

# ==========================================
# STARTUP & MAIN
# ==========================================
@app.on_event("startup")
async def startup_event():
    logging.basicConfig(level=logging.INFO)
    
    # 1. Initialize Database
    await init_db()
    if await get_mode() not in ['off', 'manual', 'auto']:
        await set_mode("off")
        
    # 2. Start Scheduler
    scheduler.add_job(auto_scrape_task, 'interval', minutes=5)
    scheduler.add_job(cleanup_old_records, 'interval', hours=24) # Daily cleanup
    scheduler.start()
    
    # 3. Start Bot Polling
    asyncio.create_task(dp.start_polling(bot))
    logging.info("🚀 Bot started successfully.")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
