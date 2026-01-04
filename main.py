import discord
from discord.ext import tasks
import aiohttp
import sqlite3
import os
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
import pytz

# ------------------------------------
# ENV
# ------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
PORT = int(os.getenv("PORT", 8080))

# ------------------------------------
# TIME (KST)
# ------------------------------------
KST = pytz.timezone("Asia/Seoul")

def now_kst():
    return datetime.utcnow().replace(tzinfo=pytz.utc).astimezone(KST)

TRACK_HOURS = [0, 12, 17]  # 12 AM, 12 PM, 5 PM KST

# ------------------------------------
# DATABASE
# ------------------------------------
db = sqlite3.connect("yt_tracker.db", check_same_thread=False)
c = db.cursor()

c.execute("""CREATE TABLE IF NOT EXISTS videos (
video_id TEXT,
title TEXT,
guild_id INTEGER,
channel_id INTEGER,
alert_channel INTEGER,
PRIMARY KEY(video_id, guild_id)
)""")

c.execute("""CREATE TABLE IF NOT EXISTS milestones (
video_id TEXT PRIMARY KEY,
last_million INTEGER DEFAULT 0,
ping TEXT
)""")

c.execute("""CREATE TABLE IF NOT EXISTS intervals (
video_id TEXT PRIMARY KEY,
hours REAL,
next_run TEXT
)""")

c.execute("""CREATE TABLE IF NOT EXISTS upcoming_alerts (
guild_id INTEGER PRIMARY KEY,
channel_id INTEGER,
ping TEXT
)""")

db.commit()

# ------------------------------------
# YOUTUBE API FETCH
# ------------------------------------
async def fetch_views(video_id):
    url = (
        "https://www.googleapis.com/youtube/v3/videos"
        f"?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as r:
            data = await r.json()
            try:
                return int(data["items"][0]["statistics"]["viewCount"])
            except:
                return None

# ------------------------------------
# FLASK KEEP-ALIVE
# ------------------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot alive 🔥"

def run_web():
    app.run(host="0.0.0.0", port=PORT)

Thread(target=run_web).start()

# ------------------------------------
# DISCORD BOT
# ------------------------------------
intents = discord.Intents.default()
bot = discord.Bot(intents=intents)

# ======================================================
# KST TRACKER LOOP (Current views + milestone + upcoming)
# ======================================================
@tasks.loop(minutes=1)
async def kst_tracker():
    now = now_kst()
    if now.hour not in TRACK_HOURS or now.minute != 0:
        return

    c.execute("SELECT video_id, title, guild_id, alert_channel FROM videos")
    videos = c.fetchall()

    upcoming_summary = {}  # guild_id -> list of lines for upcoming milestones

    for vid, title, gid, alert_ch in videos:
        views = await fetch_views(vid)
        if views is None:
            continue

        # -----------------------------
        # 1️⃣ Send current views (main tracker feature)
        # -----------------------------
        channel = bot.get_channel(alert_ch)
        if channel:
            await channel.send(f"👀 **{title}** currently has **{views:,} views**.")

        # -----------------------------
        # 2️⃣ Milestone check (secondary)
        # -----------------------------
        million = views // 1_000_000
        c.execute("SELECT last_million, ping FROM milestones WHERE video_id=?", (vid,))
        row = c.fetchone()

        if row:
            last, ping = row
            if million > last:
                if channel:
                    await channel.send(
                        f"🏆 **Milestone Alert!**\n"
                        f"**{title}** just crossed **{million}M views**!\n"
                        f"{ping or ''}"
                    )
                c.execute("UPDATE milestones SET last_million=? WHERE video_id=?", (million, vid))
                db.commit()

        # -----------------------------
        # 3️⃣ Upcoming milestone check (<100k)
        # -----------------------------
        next_m = (million + 1) * 1_000_000
        diff = next_m - views
        if diff <= 100_000:
            upcoming_summary.setdefault(gid, []).append(
                f"⏳ **{title}** is **{diff:,}** views away from **{next_m:,}**!"
            )

    # -----------------------------
    # 4️⃣ Send upcoming milestone summaries per guild
    # -----------------------------
    for gid, lines in upcoming_summary.items():
        c.execute("SELECT channel_id, ping FROM upcoming_alerts WHERE guild_id=?", (gid,))
        row = c.fetchone()
        if not row:
            continue

        ch_id, ping = row
        channel = bot.get_channel(ch_id)
        if not channel:
            continue

        for line in lines:
            await channel.send(line)

        if ping:
            await channel.send(ping)

# ======================================================
# CUSTOM INTERVAL LOOP
# ======================================================
@tasks.loop(minutes=5)
async def interval_tracker():
    now = now_kst()
    c.execute("SELECT video_id, hours, next_run FROM intervals")
    rows = c.fetchall()

    for vid, hours, next_run in rows:
        run_at = datetime.fromisoformat(next_run)
        if now < run_at:
            continue

        views = await fetch_views(vid)
        if views is None:
            continue

        c.execute(
            "UPDATE intervals SET next_run=? WHERE video_id=?",
            ((now + timedelta(hours=hours)).isoformat(), vid)
        )
        db.commit()

# ======================================================
# SLASH COMMANDS (All 14)
# ======================================================
@bot.slash_command(description="Add a video to track")
async def addvideo(ctx, video_id: str, title: str, alert_channel: discord.TextChannel):
    c.execute(
        "INSERT OR IGNORE INTO videos VALUES (?,?,?,?,?)",
        (video_id, title, ctx.guild.id, ctx.channel.id, alert_channel.id)
    )
    c.execute(
        "INSERT OR IGNORE INTO milestones VALUES (?,?,?)",
        (video_id, 0, "")
    )
    db.commit()
    await ctx.respond(f"✅ Now tracking **{title}**!")

@bot.slash_command(description="Remove a tracked video")
async def removevideo(ctx, video_id: str):
    c.execute("DELETE FROM videos WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    db.commit()
    await ctx.respond("🗑️ Video removed from tracking.")

@bot.slash_command(description="List videos in this channel")
async def listvideos(ctx):
    c.execute("SELECT title FROM videos WHERE channel_id=?", (ctx.channel.id,))
    rows = c.fetchall()
    if not rows:
        return await ctx.respond("No videos here.")
    await ctx.respond("\n".join(f"• {r[0]}" for r in rows))

@bot.slash_command(description="List all server videos")
async def serverlist(ctx):
    c.execute("SELECT title FROM videos WHERE guild_id=?", (ctx.guild.id,))
    rows = c.fetchall()
    await ctx.respond("\n".join(f"• {r[0]}" for r in rows) or "None")

@bot.slash_command(description="Force check this channel")
async def forcecheck(ctx):
    await ctx.defer()
    c.execute("SELECT title, video_id FROM videos WHERE channel_id=?", (ctx.channel.id,))
    for title, vid in c.fetchall():
        v = await fetch_views(vid)
        await ctx.send(f"📊 **{title}** — {v:,} views")

@bot.slash_command(description="Get current views for a specific video")
async def views(ctx, video_id: str):
    v = await fetch_views(video_id)
    if v is None:
        return await ctx.respond("❌ Could not fetch views.")
    await ctx.respond(f"📊 **Current Views** — {v:,} views")

@bot.slash_command(description="Show current views for all tracked videos in the server")
async def viewsall(ctx):
    await ctx.defer()
    c.execute("SELECT title, video_id FROM videos WHERE guild_id=?", (ctx.guild.id,))
    videos = c.fetchall()
    if not videos:
        return await ctx.followup.send("⚠️ No videos tracked in this server.")
    for title, vid in videos:
        v = await fetch_views(vid)
        if v is None:
            await ctx.followup.send(f"❌ {title} — could not fetch views")
        else:
            await ctx.followup.send(f"📊 **{title}** — {v:,} views")

@bot.slash_command(description="Set automatic 1M milestone alerts")
async def setmilestone(ctx, video_id: str, ping: str = ""):
    c.execute("UPDATE milestones SET ping=? WHERE video_id=?", (ping, video_id))
    db.commit()
    await ctx.respond("🎯 Milestone alerts updated!")

@bot.slash_command(description="Remove milestone alerts for a video")
async def removemilestones(ctx, video_id: str):
    c.execute("UPDATE milestones SET ping='' WHERE video_id=?", (video_id,))
    db.commit()
    await ctx.respond("❌ Milestone alerts removed.")

@bot.slash_command(description="Set custom interval (hours)")
async def setinterval(ctx, video_id: str, hours: float):
    c.execute(
        "INSERT OR REPLACE INTO intervals VALUES (?,?,?)",
        (video_id, hours, (now_kst() + timedelta(hours=hours)).isoformat())
    )
    db.commit()
    await ctx.respond("⏱️ Interval tracking enabled!")

@bot.slash_command(description="Disable the interval for a video")
async def disableinterval(ctx, video_id: str):
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    db.commit()
    await ctx.respond("❌ Interval disabled.")

@bot.slash_command(description="Setup upcoming milestone summary")
async def setupcomingmilestonesalert(ctx, channel: discord.TextChannel, ping: str = ""):
    c.execute(
        "INSERT OR REPLACE INTO upcoming_alerts VALUES (?,?,?)",
        (ctx.guild.id, channel.id, ping)
    )
    db.commit()
    await ctx.respond("📌 Upcoming milestone summary configured!")

@bot.slash_command(description="Bot health check")
async def botcheck(ctx):
    await ctx.respond(f"✅ Tracker OK — Current KST: {now_kst().strftime('%Y-%m-%d %H:%M')}")

# ------------------------------------
# READY
# ------------------------------------
@bot.event
async def on_ready():
    print(f"🚀 Logged in as {bot.user}")
    kst_tracker.start()
    interval_tracker.start()

bot.run(BOT_TOKEN)
