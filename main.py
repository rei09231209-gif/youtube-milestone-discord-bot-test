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
# DATABASE (FIXED + CLEAN)
# ------------------------------------
db = sqlite3.connect("yt_tracker.db", check_same_thread=False)
c = db.cursor()

# videos table
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

# milestones table
c.execute("""
CREATE TABLE IF NOT EXISTS milestones (
    video_id TEXT PRIMARY KEY,
    last_million INTEGER DEFAULT 0,
    ping TEXT
)
""")

# intervals table (create first)
c.execute("""
CREATE TABLE IF NOT EXISTS intervals (
    video_id TEXT PRIMARY KEY,
    hours REAL,
    next_run TEXT,
    last_views INTEGER DEFAULT 0,
    last_interval_views INTEGER DEFAULT 0
)
""")

# upcoming alerts table
c.execute("""
CREATE TABLE IF NOT EXISTS upcoming_alerts (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    ping TEXT
)
""")

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
# KST TRACKER LOOP
# ======================================================
@tasks.loop(minutes=1)
async def kst_tracker():
    now = now_kst()
    if now.hour not in TRACK_HOURS or now.minute != 0:
        return

    c.execute("SELECT video_id, title, guild_id, alert_channel FROM videos")
    videos = c.fetchall()

    upcoming_summary = {}

    for vid, title, gid, alert_ch in videos:
        views = await fetch_views(vid)
        if views is None:
            continue

        # Last stored views
        c.execute("SELECT last_views FROM intervals WHERE video_id=?", (vid,))
        r_old = c.fetchone()
        old_views = r_old[0] if r_old else None

        net = f"(+{views - old_views:,})" if old_views is not None else ""

        # Send update
        channel = bot.get_channel(alert_ch)
        if channel:
            await channel.send(
                f"📅 **{now.strftime('%Y-%m-%d %H:%M KST')}**\n"
                f"👀 **{title}** — {views:,} views {net}"
            )

        # Save views
        c.execute("""
            INSERT OR REPLACE INTO intervals (video_id, hours, next_run, last_views, last_interval_views)
            VALUES (?, COALESCE((SELECT hours FROM intervals WHERE video_id=?), 0),
                    COALESCE((SELECT next_run FROM intervals WHERE video_id=?), ?),
                    ?, ?)
        """, (vid, vid, vid, now.isoformat(), views, views))
        db.commit()

        # Milestones
        million = views // 1_000_000
        c.execute("SELECT last_million, ping FROM milestones WHERE video_id=?", (vid,))
        row = c.fetchone()

        if row:
            last_mil, ping_raw = row

            try:
                ch_id_str, ping = ping_raw.split("|", 1)
                mil_channel = bot.get_channel(int(ch_id_str))
            except:
                mil_channel = channel
                ping = ping_raw

            if million > last_mil:
                if mil_channel:
                    await mil_channel.send(
                        f"🏆 **Milestone Alert!**\n"
                        f"**{title}** just crossed **{million}M views**!\n"
                        f"{ping or ''}"
                    )
                c.execute("UPDATE milestones SET last_million=? WHERE video_id=?", (million, vid))
                db.commit()

        # Upcoming
        next_m = (million + 1) * 1_000_000
        diff = next_m - views
        if diff <= 100_000:
            upcoming_summary.setdefault(gid, []).append(
                f"⏳ **{title}** — {diff:,} views away from **{next_m:,}**!"
            )

# ======================================================
# CUSTOM INTERVAL LOOP (unchanged)
# ======================================================
@tasks.loop(minutes=5)
async def interval_tracker():
    now = now_kst()

    c.execute("SELECT video_id, hours, next_run, last_interval_views FROM intervals")
    rows = c.fetchall()

    for vid, hours, next_run, last_views in rows:
        run_at = datetime.fromisoformat(next_run)

        if now < run_at:
            continue

        views = await fetch_views(vid)
        if views is None:
            continue

        c.execute("SELECT title, alert_channel FROM videos WHERE video_id=?", (vid,))
        row = c.fetchone()
        if not row:
            continue

        title, alert_ch = row
        channel = bot.get_channel(alert_ch)

        net = views - last_views if last_views else 0
        net_text = f"(+{net:,})" if net > 0 else "(0)"

        if channel:
            await channel.send(
                f"⏱️ **Interval Track** — {now.strftime('%Y-%m-%d %H:%M KST')}\n"
                f"📌 **{title}** — {views:,} views {net_text}"
            )

        c.execute(
            "UPDATE intervals SET next_run=?, last_interval_views=? WHERE video_id=?",
            ((now + timedelta(hours=hours)).isoformat(), views, vid)
        )
        db.commit()

# ======================================================
# SLASH COMMANDS (unchanged)
# ======================================================

@bot.slash_command(description="Add a video to track")
async def addvideo(ctx, video_id: str, title: str):
    alert_channel = ctx.channel.id

    c.execute(
        "INSERT OR IGNORE INTO videos VALUES (?,?,?,?,?)",
        (video_id, title, ctx.guild.id, ctx.channel.id, alert_channel)
    )
    c.execute("INSERT OR IGNORE INTO milestones VALUES (?,?,?)", (video_id, 0, ""))
    c.execute(
        "INSERT OR IGNORE INTO intervals VALUES (?,?,?,?,?)",
        (video_id, 0, now_kst().isoformat(), 0, 0)
    )
    db.commit()

    await ctx.respond(
        f"✅ Now tracking **{title}**!\n"
        f"📌 Tracking updates will appear in <#{alert_channel}>"
    )

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
        return await ctx.respond("📭 No videos in this channel.")
    await ctx.respond("\n".join(f"• {r[0]}" for r in rows))

@bot.slash_command(description="List all server videos")
async def serverlist(ctx):
    c.execute("SELECT title FROM videos WHERE guild_id=?", (ctx.guild.id,))
    rows = c.fetchall()
    if not rows:
        return await ctx.respond("📭 No videos in this server.")
    await ctx.respond("\n".join(f"• {r[0]}" for r in rows))

@bot.slash_command(description="Force check videos in this channel")
async def forcecheck(ctx):
    await ctx.defer()

    c.execute("SELECT title, video_id FROM videos WHERE channel_id=?", (ctx.channel.id,))
    videos = c.fetchall()

    if not videos:
        return await ctx.followup.send("⚠️ No videos tracked in this channel.")

    for title, vid in videos:
        views = await fetch_views(vid)
        if views is None:
            await ctx.followup.send(f"❌ Could not fetch views for **{title}**")
            continue

        c.execute("SELECT last_views FROM intervals WHERE video_id=?", (vid,))
        row = c.fetchone()
        old = row[0] if row else 0
        net = f"(+{views - old:,})"

        c.execute("UPDATE intervals SET last_views=? WHERE video_id=?", (views, vid))
        db.commit()

        await ctx.followup.send(f"📊 **{title}** — {views:,} views {net}")

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
        views = await fetch_views(vid)
        if views is None:
            await ctx.followup.send(f"❌ {title} — could not fetch views")
            continue

        c.execute("SELECT last_views FROM intervals WHERE video_id=?", (vid,))
        row = c.fetchone()
        old = row[0] if row else 0
        net = f"(+{views - old:,})"

        c.execute("UPDATE intervals SET last_views=? WHERE video_id=?", (views, vid))
        db.commit()

        await ctx.followup.send(
            f"📊 **{title}** — {views:,} views {net}"
        )

@bot.slash_command(description="Set milestone alerts with custom channel")
async def setmilestone(ctx, video_id: str, channel: discord.TextChannel, ping: str = ""):
    combined = f"{channel.id}|{ping}"
    c.execute("UPDATE milestones SET ping=? WHERE video_id=?", (combined, video_id))
    db.commit()
    await ctx.respond(f"🏆 Milestone alerts will be sent in <#{channel.id}>!")

@bot.slash_command(description="Show recently reached 1M milestones for tracked videos")
async def reachedmilestones(ctx):
    await ctx.defer()

    c.execute("SELECT video_id, title FROM videos WHERE guild_id=?", (ctx.guild.id,))
    videos = c.fetchall()

    if not videos:
        return await ctx.followup.send("⚠️ No videos tracked in this server.")

    lines = []

    for vid, title in videos:
        c.execute("SELECT last_million FROM milestones WHERE video_id=?", (vid,))
        row = c.fetchone()
        if row and row[0] > 0:
            lines.append(f"🏆 **{title}** — reached **{row[0]}M views**")

    if not lines:
        return await ctx.followup.send("📭 No milestones have been reached yet.")

    await ctx.followup.send("🎯 **Reached Milestones**\n" + "\n".join(lines))

@bot.slash_command(description="Remove milestone alerts for a video")
async def removemilestones(ctx, video_id: str):
    c.execute("UPDATE milestones SET ping='' WHERE video_id=?", (video_id,))
    db.commit()
    await ctx.respond("❌ Milestone alerts removed.")

@bot.slash_command(description="Set custom interval (hours)")
async def setinterval(ctx, video_id: str, hours: float):
    next_time = now_kst() + timedelta(hours=hours)
    c.execute(
        "INSERT OR REPLACE INTO intervals VALUES (?,?,?,?,?)",
        (video_id, hours, next_time.isoformat(), 0, 0)
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

@bot.slash_command(description="Show upcoming milestones within 100k views")
async def upcoming(ctx):
    await ctx.response.defer()

    c.execute("SELECT title, video_id FROM videos WHERE guild_id=?", (ctx.guild.id,))
    videos = c.fetchall()

    if not videos:
        return await ctx.followup.send("⚠️ No videos tracked in this server.")

    lines = []

    for title, vid in videos:
        views = await fetch_views(vid)
        if views is None:
            continue

        million = views // 1_000_000
        next_m = (million + 1) * 1_000_000
        diff = next_m - views

        if diff <= 100_000:
            lines.append(
                f"⏳ **{title}** — {diff:,} views away from **{next_m:,}**!"
            )

    if not lines:
        return await ctx.followup.send("📭 No videos are within 100k.")

    await ctx.followup.send(
        "🏁 **Upcoming Milestones (Within 100k)**\n" + "\n".join(lines)
    )

@bot.slash_command(description="Bot health check")
async def botcheck(ctx):
    await ctx.respond(
        f"✅ Tracker OK — Current KST: {now_kst().strftime('%Y-%m-%d %H:%M')}"
    )

# ======================================================
# BOT READY
# ======================================================
@bot.event
async def on_ready():
    print(f"🚀 Logged in as {bot.user}")
    kst_tracker.start()
    interval_tracker.start()

bot.run(BOT_TOKEN)
