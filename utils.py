import aiosqlite
import aiohttp
import os
import json
from datetime import datetime, timedelta
import pytz
import re
import time
import shutil
import atexit  # Add this import

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
DB_PATH = "youtube_bot.db"
BACKUP_PATH = "backup.db"
kst = pytz.timezone('Asia/Seoul')

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Videos table (unchanged)
        await db.execute('''CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT UNIQUE,
            title TEXT,
            guild_id TEXT,
            channel_id INTEGER,
            alert_channel INTEGER
        )''')

        # FIXED intervals - ADDED alert_channel column!
        await db.execute('''CREATE TABLE IF NOT EXISTS intervals (  
            video_id TEXT,  
            guild_id TEXT,  
            hours REAL,  
            alert_channel INTEGER DEFAULT 0,  -- NEW! Channel ID per guild
            last_interval_views INTEGER DEFAULT 0,  
            last_interval_run TEXT,  
            last_fixed_run TEXT DEFAULT NULL,  -- For fixed time tracking
            kst_last_views INTEGER DEFAULT 0,  
            kst_last_run TEXT,  
            last_views INTEGER DEFAULT 0,
            view_history TEXT DEFAULT '[]',
            PRIMARY KEY (video_id, guild_id)  
        )''')  

        await db.execute('''CREATE TABLE IF NOT EXISTS milestones (  
            video_id TEXT,  
            guild_id TEXT,  
            ping TEXT DEFAULT '',  
            last_million INTEGER DEFAULT 0,  
            PRIMARY KEY (video_id, guild_id)  
        )''')  

        await db.execute('''CREATE TABLE IF NOT EXISTS server_milestones (  
            guild_id TEXT PRIMARY KEY,  
            ping TEXT  
        )''')  

        await db.execute('''CREATE TABLE IF NOT EXISTS upcoming_alerts (  
            guild_id TEXT PRIMARY KEY,  
            channel_id INTEGER,
            ping TEXT DEFAULT ''  
        )''')

        # CRITICAL: Add missing columns to existing tables (safe)
        try:
            await db.execute("ALTER TABLE intervals ADD COLUMN alert_channel INTEGER DEFAULT 0")
        except aiosqlite.OperationalError:
            pass  # Column already exists
        
        try:
            await db.execute("ALTER TABLE intervals ADD COLUMN last_fixed_run TEXT DEFAULT NULL")
        except aiosqlite.OperationalError:
            pass  # Column already exists

        # BACKFILL: Set alert_channel for existing intervals
        await db.execute("""
            UPDATE intervals 
            SET alert_channel = COALESCE(
                (SELECT alert_channel FROM videos v WHERE v.video_id = intervals.video_id AND v.guild_id = intervals.guild_id),
                0
            )
            WHERE alert_channel = 0
        """)

        await db.commit()
        print("✅ Database initialized with multi-server support!")

async def db_execute(query, params=(), fetch=False):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            if fetch:
                async with db.execute(query, params) as cursor:
                    return await cursor.fetchall()
            else:
                await db.execute(query, params)
                await db.commit()
                return True
    except Exception as e:
        print(f"DB Error: {e}")
        return False if not fetch else []

def now_kst():
    return datetime.now(kst)

# EXTRACT VIDEO ID FROM URL OR ID
def extract_video_id(url_or_id):
    if len(url_or_id) == 11:
        return url_or_id
    patterns = [
        r'(?:v=|/)([0-9A-Za-z_-]{11}).*',
        r'(?:embed/)([0-9A-Za-z_-]{11})',
        r'(?:/watch?v=)([0-9A-Za-z_-]{11})',
        r'(?:youtu.be/)([0-9A-Za-z_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    return None

async def fetch_video_stats(video_id):
    """Fetch views + likes for video"""
    try:
        if not YOUTUBE_API_KEY:
            print("❌ Missing YOUTUBE_API_KEY")
            return None, None
        url = f"https://www.googleapis.com/youtube/v3/videos?id={video_id}&part=statistics&key={YOUTUBE_API_KEY}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if data.get('items'):
                    stats = data['items'][0]['statistics']
                    views = int(stats.get('viewCount', 0))
                    likes = int(stats.get('likeCount', 0))
                    return views, likes
                return None, None
    except Exception as e:
        print(f"Stats fetch error: {e}")
        return None, None

# FIXED: Proper guild+channel check
async def ensure_video_exists(video_id, guild_id, title="", alert_channel=None, channel_id=None):
    """Ensure video exists FOR THIS GUILD with correct channels"""
    exists = await db_execute(
        "SELECT 1 FROM videos WHERE video_id=? AND guild_id=?", 
        (video_id, guild_id), 
        fetch=True
    )
    if exists:
        return

    # FETCH VIDEO TITLE IF NEEDED (placeholder - add your fetch_video_title if exists)
    if not title:
        title = video_id  # Fallback

    alert_ch = alert_channel or channel_id
    await db_execute("""
        INSERT INTO videos (video_id, title, guild_id, alert_channel, channel_id) 
        VALUES (?, ?, ?, ?, ?)
    """, (video_id, title, guild_id, alert_ch or 0, channel_id or 0))

async def get_real_growth_rate(video_id, guild_id):
    """Calculate real growth rate from DB history"""
    history_data = await db_execute(
        "SELECT view_history FROM intervals WHERE video_id=? AND guild_id=?", 
        (video_id, guild_id), fetch=True
    )
    if not history_data:
        return 100

    try:
        history = json.loads(history_data[0]['view_history']) if history_data[0]['view_history'] != '[]' else []
        if len(history) < 2:
            return 100

        recent = sorted(history, key=lambda x: x['time'])[-2:]
        if len(recent) < 2:
            return 100

        old_views = recent[0]['views']
        new_views = recent[1]['views']
        time_diff = (datetime.fromisoformat(recent[1]['time']) - datetime.fromisoformat(recent[0]['time'])).total_seconds() / 3600

        if time_diff > 0:
            growth_rate = (new_views - old_views) / time_diff
            return max(10, growth_rate)
    except:
        pass
    return 100

# === NEW DB BACKUP/RESTORE FUNCTIONS ===
def backup_db():
    try:
        if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 1024:
            shutil.copy2(DB_PATH, BACKUP_PATH)
            print(f"✅ DB backed up ({os.path.getsize(DB_PATH)/1024:.1f}KB)")
        else:
            print("⚠️ DB too small - skipped backup")
    except Exception as e:
        print(f"❌ Backup failed: {e}")

def restore_db():
    try:
        if os.path.exists(BACKUP_PATH) and os.path.getsize(BACKUP_PATH) > 1024:
            if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) < 1024:
                shutil.copy2(BACKUP_PATH, DB_PATH)
                print(f"✅ Restored DB ({os.path.getsize(BACKUP_PATH)/1024:.1f}KB)")
                return True
    except Exception as e:
        print(f"❌ Restore failed: {e}")
    return False