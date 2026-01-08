import aiosqlite
import aiohttp
import os
from datetime import datetime, timedelta
import pytz
import asyncio
from dotenv import load_dotenv

load_dotenv()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
DB_PATH = os.getenv("DB_PATH", "/opt/render/project/src/yt_data.db")
KST = pytz.timezone("Asia/Seoul")

db_lock = asyncio.Lock()
youtube_semaphore = asyncio.Semaphore(5)

def now_kst():
    return datetime.now(KST)

TRACK_HOURS = [0, 12, 17]

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS videos (
            key TEXT PRIMARY KEY, video_id TEXT, title TEXT, guild_id TEXT,
            channel_id TEXT, alert_channel TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS intervals (
            video_id TEXT PRIMARY KEY, hours REAL DEFAULT 0, next_run TEXT,
            last_views INTEGER DEFAULT 0, last_interval_views INTEGER DEFAULT 0,
            last_interval_run TEXT, kst_last_views INTEGER DEFAULT 0, kst_last_run TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS milestones (
            video_id TEXT PRIMARY KEY, last_million INTEGER DEFAULT 0, ping TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS upcoming_alerts (
            guild_id TEXT PRIMARY KEY, channel_id TEXT, ping TEXT
        )''')
        await db.commit()
    print(f"✅ SQLite3 initialized: {DB_PATH}")

async def db_execute(query, params=(), fetch=False):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                async with db.execute(query, params) as cursor:
                    if fetch:
                        return await cursor.fetchall()
                await db.commit()
                return True
            except Exception as e:
                print(f"❌ DB Error: {e}")
                return False

async def fetch_views(video_id):
    async with youtube_semaphore:
        url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
        for attempt in range(3):
            try:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as r:
                        if r.status != 200:
                            await asyncio.sleep(1)
                            continue
                        data = await r.json()
                        if not data.get("items"):
                            return None
                        return int(data["items"][0]["statistics"]["viewCount"])
            except:
                await asyncio.sleep(1 + attempt * 0.5)
        return None

def estimate_eta(current_views, target_views):
    remaining = target_views - current_views
    if remaining <= 0: return "NOW!"
    hours = max(1, remaining / 1000)
    if hours < 24: return f"{int(hours)}hr"
    return f"{int(hours/24)}d"

async def ensure_video_exists(video_id, guild_id, title="", channel_id=0, alert_channel=0):
    key = f"{video_id}_{guild_id}"
    exists = await db_execute("SELECT 1 FROM videos WHERE key=?", (key,), True)
    if not exists:
        await db_execute('''INSERT INTO videos (key, video_id, title, guild_id, channel_id, alert_channel)
                          VALUES (?, ?, ?, ?, ?, ?)''',
                        (key, video_id, title or video_id, guild_id, channel_id, alert_channel))
        await db_execute('''INSERT OR IGNORE INTO intervals (video_id) VALUES (?)''', (video_id,))
        await db_execute('''INSERT OR IGNORE INTO milestones (video_id) VALUES (?)''', (video_id,))
