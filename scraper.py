# scraper.py
import asyncio
import aiohttp
import logging
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

BASE_URL = "https://xyzabc.com/"  # <-- real site yaha

VIDEO_CONTAINER_SELECTOR = "div.post"
VIDEO_LINK_SELECTOR = "a"
THUMBNAIL_SELECTOR = "img"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
}

logger = logging.getLogger(__name__)

async def fetch_html(session, url):
    try:
        async with session.get(url, headers=HEADERS, timeout=15) as r:
            if r.status == 200:
                return await r.text()
    except Exception as e:
        logger.error(f"Fetch error: {e}")
    return None

def extract_video_info(html, search_query=None):
    soup = BeautifulSoup(html, "html.parser")
    videos = []

    for box in soup.select(VIDEO_CONTAINER_SELECTOR):
        try:
            link = box.select_one(VIDEO_LINK_SELECTOR)
            img = box.select_one(THUMBNAIL_SELECTOR)

            if not link or not link.get("href"):
                continue

            video_url = urljoin(BASE_URL, link["href"])
            video_id = urlparse(video_url).path
            thumb = urljoin(BASE_URL, img["src"]) if img and img.get("src") else None

            if search_query:
                text = link.get_text(" ").lower()
                if search_query.lower() not in text:
                    continue

            videos.append({
                "id": video_id,
                "url": video_url,
                "thumbnail": thumb
            })
        except Exception as e:
            logger.error(f"Parse error: {e}")

    return videos

async def scrape_site(session, search_query=None):
    html = await fetch_html(session, BASE_URL)
    if not html:
        return []
    return extract_video_info(html, search_query)