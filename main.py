# ===============================
# YOUTUBE TRACKER BOT (PYCORD 2.4.1 + PYTHON 3.11)
# FULL FINAL VERSION
# ===============================

import discord
from discord.ext import tasks
import sqlite3
import aiohttp
import os
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread

# ============= ENVIRONMENT =============
BOT_TOKEN = os.getenv("BOT_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
PORT = int(os.getenv("PORT", 10000))

# ============= KST TIME =============
def now_kst():
    return datetime.utcnow() + timedelta(hours=9)

TRACK_HOURS = [0, 12, 17]  # Midnight, noon, 5 PM KST


# ============= DATABASE =============
db = sqlite3.connect("yt_tracker.db", check_same_thread=False)
c = db.cursor()

# Videos tracked per channel
c.execute("""
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT,
    title TEXT,
    guild_id TEXT,
    channel_id TEXT,
    PRIMARY KEY(video_id, channel_id)
)
""")

# Milestone tracking
c.execute("""
CREATE TABLE IF NOT EXISTS milestones (
    video_id TEXT PRIMARY KEY,
    last_million INTEGER DEFAULT 0,
    last_alerted_time TEXT,
    ping_message TEXT,
    alert_channel TEXT
)
""")

# Custom interval checks
c.execute("""
CREATE TABLE IF NOT EXISTS intervals (
    video_id TEXT PRIMARY KEY,
    interval_hours REAL,
    next_run TEXT
)
""")

# Upcoming milestone summaries
c.execute("""
CREATE TABLE IF NOT EXISTS upcoming_alerts (
    guild_id TEXT PRIMARY KEY,
    channel_id TEXT,
    ping_message TEXT
)
""")

db.commit()


# ============= YOUTUBE API =============
async def fetch_views(video_id: str):
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


# ============= KEEP ALIVE SERVER =============
app = Flask("keepalive")

@app.route("/")
def home():
    return "Bot alive 🔥"

def run_web():
    app.run(host="0.0.0.0", port=PORT)

Thread(target=run_web).start()


# ============= BOT INSTANCE =============
intents = discord.Intents.default()
bot = discord.Bot(intents=intents)


# ============= ON READY =============
@bot.event
async def on_ready():
    print(f"🚀 Logged in as {bot.user}")
    kst_tracker.start()
    custom_interval_loop.start()


# ============================================================
#                 HOURLY KST TRACKER (0, 12, 17)
# ============================================================
@tasks.loop(minutes=1)
async def kst_tracker():
    now = now_kst()
    if now.hour not in TRACK_HOURS or now.minute != 0:
        return

    c.execute("SELECT video_id, title, guild_id, channel_id FROM videos")
    rows = c.fetchall()

    upcoming_summary = {}  # guild_id → list of lines

    for vid, title, gid, ch_id in rows:
        views = await fetch_views(vid)
        if views is None:
            continue

        # ======== MILESTONE CHECK ========
        c.execute("SELECT last_million, ping_message, alert_channel FROM milestones WHERE video_id=?", (vid,))
        row = c.fetchone()

        if row:
            last_million, ping, alert_channel = row
            current_million = views // 1_000_000

            if current_million > last_million:
                real_channel = None

                if alert_channel:
                    real_channel = bot.get_channel(int(alert_channel))

                if real_channel is None:
                    real_channel = bot.get_channel(int(ch_id))

                if real_channel:
                    await real_channel.send(
                        f"🏆 **{title}** just crossed **{current_million}M views!**\n"
                        f"{ping or ''}"
                    )

                c.execute("""
                    UPDATE milestones
                    SET last_million=?, last_alerted_time=?
                    WHERE video_id=?
                """, (current_million, now.isoformat(), vid))
                db.commit()

        # ======== UPCOMING CHECK (≤100k away) ========
        next_m = ((views // 1_000_000) + 1) * 1_000_000
        diff = next_m - views
        if diff <= 100_000:
            upcoming_summary.setdefault(gid, []).append(
                f"🎯 **{title}** — `{diff:,}` views left to **{next_m:,}**"
            )

    # ======== SEND UPCOMING SUMMARY ========
    for gid, lines in upcoming_summary.items():
        c.execute("SELECT channel_id, ping_message FROM upcoming_alerts WHERE guild_id=?", (gid,))
        row = c.fetchone()
        if not row:
            continue

        summary_channel, ping = row
        ch = bot.get_channel(int(summary_channel))
        if not ch:
            continue

        # One message per video
        for l in lines:
            await ch.send(l)

        # Ping at the end
        if ping:
            await ch.send(ping)


# ============================================================
#                  CUSTOM INTERVAL TRACKER
# ============================================================
@tasks.loop(minutes=5)
async def custom_interval_loop():
    now = now_kst()

    c.execute("SELECT video_id, interval_hours, next_run FROM intervals")
    for vid, hrs, nxt in c.fetchall():
        if now < datetime.fromisoformat(nxt):
            continue

        views = await fetch_views(vid)
        if views is None:
            continue

        next_time = now + timedelta(hours=hrs)
        c.execute("UPDATE intervals SET next_run=? WHERE video_id=?", (next_time.isoformat(), vid))
        db.commit()


# ============================================================
#                          COMMANDS
# ============================================================

# -------- ADD VIDEO --------
@bot.slash_command(description="Add a video to track in this channel")
async def addvideo(ctx, video_id: str, title: str):
    c.execute("""
        INSERT OR IGNORE INTO videos VALUES (?, ?, ?, ?)
    """, (video_id, title, str(ctx.guild.id), str(ctx.channel.id)))

    c.execute("""
        INSERT OR IGNORE INTO milestones VALUES (?, 0, NULL, '', NULL)
    """, (video_id,))

    db.commit()
    await ctx.respond(f"📌 Tracking **{title}** in this channel!", ephemeral=True)


# -------- REMOVE VIDEO --------
@bot.slash_command()
async def removevideo(ctx, video_id: str):
    c.execute("DELETE FROM videos WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    db.commit()
    await ctx.respond("🗑️ Video removed.", ephemeral=True)


# -------- LIST VIDEOS IN CHANNEL --------
@bot.slash_command()
async def listvideos(ctx):
    c.execute("SELECT title FROM videos WHERE channel_id=?", (str(ctx.channel.id),))
    rows = c.fetchall()
    msg = "\n".join(f"📺 {x[0]}" for x in rows) if rows else "None"
    await ctx.respond(msg, ephemeral=True)


# -------- LIST ALL SERVER VIDEOS --------
@bot.slash_command()
async def serverlist(ctx):
    c.execute("SELECT title FROM videos WHERE guild_id=?", (str(ctx.guild.id),))
    rows = c.fetchall()
    msg = "\n".join(f"📺 {x[0]}" for x in rows) if rows else "None"
    await ctx.respond(msg, ephemeral=True)


# -------- FORCE CHECK --------
@bot.slash_command()
async def forcecheck(ctx):
    await ctx.defer()
    c.execute("SELECT title, video_id FROM videos WHERE channel_id=?", (str(ctx.channel.id),))
    rows = c.fetchall()
    for title, vid in rows:
        views = await fetch_views(vid)
        await ctx.followup.send(f"🔎 **{title}** → `{views:,}` views")


# -------- VIEWS FOR ONE VIDEO --------
@bot.slash_command()
async def views(ctx, video_id: str):
    v = await fetch_views(video_id)
    await ctx.respond(f"👀 `{v:,}` views")


# -------- VIEWS FOR ALL SERVER VIDEOS --------
@bot.slash_command()
async def viewsall(ctx):
    await ctx.defer()
    c.execute("SELECT title, video_id FROM videos WHERE guild_id=?", (str(ctx.guild.id),))
    for title, vid in c.fetchall():
        v = await fetch_views(vid)
        await ctx.followup.send(f"📊 **{title}** → `{v:,}` views")


# -------- SET MILESTONE ALERT CHANNEL & PING --------
@bot.slash_command()
async def setmilestonealert(ctx, video_id: str, channel: discord.TextChannel, ping_message: str = ""):
    c.execute("""
        UPDATE milestones
        SET alert_channel=?, ping_message=?
        WHERE video_id=?
    """, (str(channel.id), ping_message, video_id))

    db.commit()
    await ctx.respond("🏁 Milestone alert updated!", ephemeral=True)


# -------- REMOVE MILESTONE ALERT --------
@bot.slash_command()
async def removemilestonealert(ctx, video_id: str):
    c.execute("""
        UPDATE milestones
        SET alert_channel=NULL, ping_message=''
        WHERE video_id=?
    """, (video_id,))
    db.commit()
    await ctx.respond("❌ Milestone alert removed.", ephemeral=True)


# -------- REACHED MILESTONES (LAST 24 HOURS) --------
@bot.slash_command()
async def reachedmilestones(ctx):
    cutoff = now_kst() - timedelta(hours=24)

    c.execute("""
        SELECT video_id, last_million, last_alerted_time
        FROM milestones
        WHERE last_alerted_time IS NOT NULL
    """)

    results = []
    for vid, million, dt in c.fetchall():
        t = datetime.fromisoformat(dt)
        if t >= cutoff:
            results.append(f"🏆 `{vid}` → **{million}M views**")

    await ctx.respond("\n".join(results) if results else "None in last 24 hours", ephemeral=True)


# -------- UPCOMING MILESTONES --------
@bot.slash_command()
async def upcomingmilestones(ctx):
    await ctx.defer()

    c.execute("SELECT title, video_id FROM videos WHERE guild_id=?", (str(ctx.guild.id),))
    for title, vid in c.fetchall():
        v = await fetch_views(vid)
        next_m = ((v // 1_000_000) + 1) * 1_000_000
        diff = next_m - v
        if diff <= 100_000:
            await ctx.followup.send(
                f"🎯 **{title}** — `{diff:,}` views left to **{next_m:,}**"
            )


# -------- SET UPCOMING MILESTONE ALERT --------
@bot.slash_command()
async def setupcomingmilestonesalert(ctx, channel: discord.TextChannel, ping_message: str = ""):
    c.execute("""
        INSERT OR REPLACE INTO upcoming_alerts VALUES (?, ?, ?)
    """, (str(ctx.guild.id), str(channel.id), ping_message))

    db.commit()
    await ctx.respond("📢 Upcoming milestone alerts configured!", ephemeral=True)


# -------- CUSTOM INTERVAL --------
@bot.slash_command()
async def setinterval(ctx, video_id: str, interval_hours: float):
    nxt = now_kst() + timedelta(hours=interval_hours)

    c.execute("""
        INSERT OR REPLACE INTO intervals VALUES (?, ?, ?)
    """, (video_id, interval_hours, nxt.isoformat()))

    db.commit()
    await ctx.respond("⏱️ Interval set.", ephemeral=True)


@bot.slash_command()
async def disableinterval(ctx, video_id: str):
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    db.commit()
    await ctx.respond("🚫 Interval disabled.", ephemeral=True)


# -------- HEALTH CHECK --------
@bot.slash_command()
async def botcheck(ctx):
    await ctx.respond(f"🤖 Bot OK — {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


# ============= RUN BOT =============
bot.run(BOT_TOKEN)
