# main.py
import os
import asyncio
import logging
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import (
    init_db, is_user_verified, verify_user,
    set_auto_mode, is_any_auto_enabled,
    is_video_sent, save_sent_video
)
from scraper import scrape_site

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_IDS = os.getenv("GROUP_IDS").split(",")

TARGET_GROUP_AUTO = int(GROUP_IDS[0])
TARGET_GROUP_SEARCH = int(GROUP_IDS[1])

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler()

def main_keyboard():
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="1️⃣ Gost auto 24/7")],
            [types.KeyboardButton(text="2️⃣ Search")]
        ],
        resize_keyboard=True
    )

def delete_kb():
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="Delete ❌", callback_data="del")]]
    )

@dp.message(CommandStart())
async def start(m: types.Message):
    if not await is_user_verified(m.from_user.id):
        await m.answer("Keyword bhejo")
    else:
        await m.answer("Mode select karo", reply_markup=main_keyboard())

@dp.message()
async def text_handler(m: types.Message):
    uid = m.from_user.id
    txt = m.text.strip()

    if not await is_user_verified(uid):
        if txt == "Fxuniverse":
            await verify_user(uid)
            await m.answer("Access granted", reply_markup=main_keyboard())
        else:
            await m.answer("Wrong keyword")
        return

    if txt == "1️⃣ Gost auto 24/7":
        await set_auto_mode(uid, True)
        await m.answer("Auto mode ON")
        return

    if txt == "2️⃣ Search":
        await m.answer("Search word bhejo")
        return

    async with aiohttp.ClientSession() as session:
        results = await scrape_site(session, txt)

        for v in results:
            if await is_video_sent(v["id"]):
                continue

            if v["thumbnail"]:
                await bot.send_photo(
                    TARGET_GROUP_SEARCH,
                    v["thumbnail"],
                    caption=v["url"],
                    reply_markup=delete_kb()
                )
            else:
                await bot.send_message(
                    TARGET_GROUP_SEARCH,
                    v["url"],
                    reply_markup=delete_kb()
                )

            await save_sent_video(v["id"])
            await asyncio.sleep(3)

@dp.callback_query(F.data == "del")
async def delete_msg(c: types.CallbackQuery):
    await c.message.delete()
    await c.answer()

async def auto_scrape():
    if not await is_any_auto_enabled():
        return

    async with aiohttp.ClientSession() as session:
        videos = await scrape_site(session)

        for v in videos:
            if await is_video_sent(v["id"]):
                continue

            await bot.send_photo(
                TARGET_GROUP_AUTO,
                v["thumbnail"],
                caption=v["url"],
                reply_markup=delete_kb()
            )

            await save_sent_video(v["id"])
            await asyncio.sleep(3)

async def startup():
    await init_db()
    scheduler.add_job(auto_scrape, "interval", minutes=5)
    scheduler.start()

async def main():
    await startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())