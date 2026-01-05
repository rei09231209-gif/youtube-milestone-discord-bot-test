import discord
from discord.ext import tasks, commands
from discord import app_commands
import aiohttp
import sqlite3
import os
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
import pytz
import asyncio

# ========================================
# ENV
# ========================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
PORT = int(os.getenv("PORT", 8080))

if not BOT_TOKEN or not YOUTUBE_API_KEY:
    raise ValueError("❌ Missing BOT_TOKEN or YOUTUBE_API_KEY")

# ========================================
# TIME (KST)
# ========================================
KST = pytz.timezone("Asia/Seoul")

def now_kst():
    return datetime.now(KST)

TRACK_HOURS = [0, 12, 17]  # 12AM, 12PM, 5PM KST

# ========================================
# DATABASE (THREAD-SAFE)
# ========================================
def get_db():
    """Thread-safe DB connection."""
    conn = sqlite3.connect("yt_tracker.db", timeout=10)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

# Initialize tables
with get_db() as db:
    c = db.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS videos (
        video_id TEXT,
        title TEXT,
        guild_id INTEGER,
        channel_id INTEGER,
        alert_channel INTEGER,
        PRIMARY KEY(video_id, guild_id)
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS milestones (
        video_id TEXT PRIMARY KEY,
        last_million INTEGER DEFAULT 0,
        ping TEXT DEFAULT ''
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS intervals (
        video_id TEXT PRIMARY KEY,
        hours REAL DEFAULT 0,
        next_run TEXT,
        last_views INTEGER DEFAULT 0,
        last_interval_views INTEGER DEFAULT 0
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS upcoming_alerts (
        guild_id INTEGER PRIMARY KEY,
        channel_id INTEGER,
        ping TEXT DEFAULT ''
    )
    """)
    db.commit()

def db_execute(query, params=(), fetch=False):
    """Safe DB execute with proper returns."""
    with get_db() as db:
        c = db.cursor()
        c.execute(query, params)
        db.commit()
        if fetch and query.strip().upper().startswith('SELECT'):
            return c.fetchall()
        return True

def db_fetch(query, params=()):
    """DB fetch wrapper."""
    return db_execute(query, params, fetch=True)

# ========================================
# YOUTUBE API
# ========================================
async def fetch_views(video_id):
    """Fetch views with retries."""
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
        except Exception:
            await asyncio.sleep(1 + attempt * 0.5)
    return None

# ========================================
# FLASK KEEPALIVE
# ========================================
app = Flask(__name__)

@app.route("/")
def home():
    return {"status": "alive", "time": now_kst().isoformat()}

@app.route("/health")
def health():
    return {"db": "ok", "status": "running"}

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False)

# ========================================
# DISCORD BOT
# ========================================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

# ========================================
# KST TRACKER (12AM, 12PM, 5PM KST)
# ========================================
@tasks.loop(minutes=1)
async def kst_tracker():
    try:
        now = now_kst()
        # 2-minute window for reliability
        if now.hour not in TRACK_HOURS or now.minute > 1:
            return
        
        print(f"🔄 KST Check: {now.strftime('%H:%M')}")
        videos = db_fetch("SELECT video_id, title, guild_id, alert_channel FROM videos")
        
        for vid, title, gid, alert_ch in videos:
            views = await fetch_views(vid)
            if views is None:
                continue

            # Net change
            old_data = db_fetch("SELECT last_views FROM intervals WHERE video_id=?", (vid,))
            old_views = old_data[0][0] if old_data else 0
            net = f"(+{views-old_views:,})" if old_views else ""

            # Send update
            channel = bot.get_channel(alert_ch)
            if channel:
                await channel.send(
                    f"📅 **{now.strftime('%Y-%m-%d %H:%M KST')}**\n"
                    f"👀 **{title}** — {views:,} views {net}"
                )

            # Save views
            db_execute("INSERT OR REPLACE INTO intervals (video_id, hours, next_run, last_views) VALUES (?, 0, ?, ?)",
                      (vid, now.isoformat(), views))

            # Check milestones
            million = views // 1_000_000
            milestone_data = db_fetch("SELECT last_million, ping FROM milestones WHERE video_id=?", (vid,))
            
            if milestone_data and million > milestone_data[0][0] and milestone_data[0][1]:
                last_mil, ping_raw = milestone_data[0]
                try:
                    ch_id, ping_msg = ping_raw.split("|", 1)
                    mil_channel = bot.get_channel(int(ch_id))
                except:
                    mil_channel = channel
                    ping_msg = ping_raw
                
                if mil_channel:
                    await mil_channel.send(
                        f"🏆 **{title}** crossed **{million}M views**!\n{ping_msg}"
                    )
                
                db_execute("UPDATE milestones SET last_million=? WHERE video_id=?", (million, vid))

    except Exception as e:
        print(f"❌ KST Tracker Error: {e}")

# ========================================
# INTERVAL TRACKER (Every 5min)
# ========================================
@tasks.loop(minutes=5)
async def tracking_loop():
    try:
        now = now_kst()
        rows = db_fetch("SELECT video_id, hours, next_run, last_interval_views FROM intervals WHERE hours > 0")
        
        for video_id, hours, next_run, last_interval_views in rows:
            try:
                next_run_dt = datetime.fromisoformat(next_run).replace(tzinfo=KST)
                if now < next_run_dt:
                    continue
            except:
                continue

            video_data = db_fetch("SELECT title, channel_id FROM videos WHERE video_id=?", (video_id,))
            if not video_data:
                continue

            title, ch_id = video_data[0]
            channel = bot.get_channel(ch_id)
            if not channel:
                continue

            views = await fetch_views(video_id)
            if views is None:
                continue

            net = views - last_interval_views if last_interval_views else 0
            next_time = now + timedelta(hours=hours)

            await channel.send(
                f"⏱️ **{title}** Interval Update\n"
                f"📊 {views:,} views (+{net:,})\n"
                f"⏳ Next: {hours}hrs"
            )

            db_execute("UPDATE intervals SET next_run=?, last_interval_views=? WHERE video_id=?",
                      (next_time.isoformat(), views, video_id))

    except Exception as e:
        print(f"❌ Interval Error: {e}")

# ========================================
# ALL 15 SLASH COMMANDS
# ========================================
@bot.tree.command(name="addvideo", description="Add video to track")
@app_commands.describe(video_id="YouTube video ID", title="Video title")
async def addvideo(interaction: discord.Interaction, video_id: str, title: str):
    db_execute("INSERT OR IGNORE INTO videos VALUES (?,?,?,?,?)", 
               (video_id, title, interaction.guild.id, interaction.channel.id, interaction.channel.id))
    db_execute("INSERT OR IGNORE INTO milestones VALUES (?,?,?)", (video_id, 0, ""))
    db_execute("INSERT OR IGNORE INTO intervals VALUES (?,?,?,?,?)", 
               (video_id, 0, now_kst().isoformat(), 0, 0))
    await interaction.response.send_message(f"✅ Tracking **{title}** in <#{interaction.channel.id}>")

@bot.tree.command(name="removevideo", description="Remove video")
@app_commands.describe(video_id="YouTube video ID")
async def removevideo(interaction: discord.Interaction, video_id: str):
    db_execute("DELETE FROM videos WHERE video_id=?", (video_id,))
    db_execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    db_execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    await interaction.response.send_message("🗑️ Video removed")

@bot.tree.command(name="listvideos", description="Videos in this channel")
async def listvideos(interaction: discord.Interaction):
    rows = db_fetch("SELECT title FROM videos WHERE channel_id=?", (interaction.channel.id,))
    if not rows:
        await interaction.response.send_message("📭 No videos here")
    else:
        await interaction.response.send_message("\n".join(f"• {r[0]}" for r in rows))

@bot.tree.command(name="serverlist", description="All server videos")
async def serverlist(interaction: discord.Interaction):
    rows = db_fetch("SELECT title FROM videos WHERE guild_id=?", (interaction.guild.id,))
    if not rows:
        await interaction.response.send_message("📭 No server videos")
    else:
        await interaction.response.send_message("\n".join(f"• {r[0]}" for r in rows))

@bot.tree.command(name="forcecheck", description="Force check channel videos")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = db_fetch("SELECT title, video_id FROM videos WHERE channel_id=?", (interaction.channel.id,))
    
    if not videos:
        return await interaction.followup.send("⚠️ No videos")
    
    for title, vid in videos:
        views = await fetch_views(vid)
        if views:
            old = db_fetch("SELECT last_views FROM intervals WHERE video_id=?", (vid,))
            old_views = old[0][0] if old else 0
            db_execute("UPDATE intervals SET last_views=? WHERE video_id=?", (views, vid))
            await interaction.followup.send(f"📊 **{title}**: {views:,} (+{views-old_views:,})")
        else:
            await interaction.followup.send(f"❌ **{title}**: fetch failed")

@bot.tree.command(name="views", description="Single video views")
@app_commands.describe(video_id="YouTube video ID")
async def views(interaction: discord.Interaction, video_id: str):
    v = await fetch_views(video_id)
    if v:
        await interaction.response.send_message(f"📊 {v:,} views")
    else:
        await interaction.response.send_message("❌ Fetch failed")

@bot.tree.command(name="viewsall", description="All server videos")
async def viewsall(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = db_fetch("SELECT title, video_id FROM videos WHERE guild_id=?", (interaction.guild.id,))
    
    if not videos:
        return await interaction.followup.send("⚠️ No videos")
    
    for title, vid in videos:
        views = await fetch_views(vid)
        if views:
            old = db_fetch("SELECT last_views FROM intervals WHERE video_id=?", (vid,))
            old_views = old[0][0] if old else 0
            db_execute("UPDATE intervals SET last_views=? WHERE video_id=?", (views, vid))
            await interaction.followup.send(f"📊 **{title}**: {views:,} (+{views-old_views:,})")

@bot.tree.command(name="setmilestone", description="Milestone alerts")
@app_commands.describe(video_id="Video ID", channel="Alert channel", ping="Ping message")
async def setmilestone(interaction: discord.Interaction, video_id: str, channel: discord.TextChannel, ping: str = ""):
    ping_data = f"{channel.id}|{ping}"
    db_execute("INSERT OR REPLACE INTO milestones (video_id, ping) VALUES (?, ?)", (video_id, ping_data))
    await interaction.response.send_message(f"🏆 Alerts set for <#{channel.id}>")

@bot.tree.command(name="reachedmilestones", description="Show hit milestones")
async def reachedmilestones(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = db_fetch("SELECT title, video_id FROM videos WHERE guild_id=?", (interaction.guild.id,))
    lines = []
    
    for title, vid in videos:
        data = db_fetch("SELECT last_million FROM milestones WHERE video_id=?", (vid,))
        if data and data[0][0] > 0:
            lines.append(f"🏆 **{title}**: {data[0][0]}M")
    
    if not lines:
        await interaction.followup.send("📭 No milestones yet")
    else:
        await interaction.followup.send("\n".join(lines))

@bot.tree.command(name="removemilestones", description="Clear milestone alerts")
@app_commands.describe(video_id="Video ID")
async def removemilestones(interaction: discord.Interaction, video_id: str):
    db_execute("UPDATE milestones SET ping='' WHERE video_id=?", (video_id,))
    await interaction.response.send_message("❌ Alerts cleared")

@bot.tree.command(name="setinterval", description="Custom update interval")
@app_commands.describe(video_id="Video ID", hours="Hours between checks")
async def setinterval(interaction: discord.Interaction, video_id: str, hours: float):
    next_time = now_kst() + timedelta(hours=hours)
    db_execute("INSERT OR REPLACE INTO intervals VALUES (?,?,?,?,?)",
               (video_id, hours, next_time.isoformat(), 0, 0))
    await interaction.response.send_message(f"⏱️ Interval set: {hours}hrs")

@bot.tree.command(name="disableinterval", description="Stop interval updates")
@app_commands.describe(video_id="Video ID")
async def disableinterval(interaction: discord.Interaction, video_id: str):
    db_execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    await interaction.response.send_message("⏹️ Intervals stopped")

@bot.tree.command(name="setupcomingmilestonesalert", description="Upcoming summary")
@app_commands.describe(channel="Summary channel", ping="Ping message")
async def setupcomingmilestonesalert(interaction: discord.Interaction, channel: discord.TextChannel, ping: str = ""):
    db_execute("INSERT OR REPLACE INTO upcoming_alerts VALUES (?,?,?)", 
               (interaction.guild.id, channel.id, ping))
    await interaction.response.send_message("📌 Summary alerts configured")

@bot.tree.command(name="upcoming", description="Videos near milestones")
async def upcoming(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = db_fetch("SELECT title, video_id FROM videos WHERE guild_id=?", (interaction.guild.id,))
    lines = []
    
    for title, vid in videos:
        views = await fetch_views(vid)
        if views:
            next_m = ((views // 1_000_000) + 1) * 1_000_000
            diff = next_m - views
            if diff <= 100_000:
                lines.append(f"⏳ **{title}**: {diff:,} to {next_m:,}")
    
    if not lines:
        await interaction.followup.send("📭 Nothing near milestones")
    else:
        await interaction.followup.send("\n".join(lines))

@bot.tree.command(name="botcheck", description="Bot status")
async def botcheck(interaction: discord.Interaction):
    await interaction.response.send_message(f"✅ KST: {now_kst().strftime('%Y-%m-%d %H:%M')}")

# ========================================
# BOT EVENTS
# ========================================
@bot.event
async def on_ready():
    print(f"🚀 {bot.user} online - KST: {now_kst().strftime('%H:%M')}")
    
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        print(f"❌ Sync error: {e}")
    
    kst_tracker.start()
    tracking_loop.start()
    print("⏱️ Trackers started")
    
    # Start Flask AFTER bot is ready
    Thread(target=run_flask, daemon=True).start()
    print("🌐 Flask started")

@bot.event
async def on_error(event, *args, **kwargs):
    print(f"❌ Error in {event}: {args}")

# ========================================
# START
# ========================================
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
