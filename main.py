import os
import time
import asyncio
import sqlite3
import requests
import discord

from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

# ---------------- CONFIG ----------------
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is not set")
if not YOUTUBE_API_KEY:
    raise RuntimeError("YOUTUBE_API_KEY is not set")

KST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------- KEEP ALIVE (RENDER) ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive"

def run_web():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_web, daemon=True).start()

# ---------------- DATABASE ----------------
conn = sqlite3.connect("tracker.db", check_same_thread=False)
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    title TEXT,
    guild_id INTEGER,
    channel_id INTEGER,
    last_views INTEGER DEFAULT 0,
    last_checked INTEGER DEFAULT 0
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS milestones (
    video_id TEXT,
    milestone INTEGER,
    ping TEXT,
    PRIMARY KEY (video_id, milestone)
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS reached_milestones (
    video_id TEXT,
    milestone INTEGER,
    reached_at INTEGER,
    PRIMARY KEY (video_id, milestone)
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS intervals (
    video_id TEXT PRIMARY KEY,
    interval_hours INTEGER,
    next_run INTEGER
)
""")

conn.commit()

# ---------------- HELPERS ----------------
def format_views(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def get_views(video_id):
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "statistics",
        "id": video_id,
        "key": YOUTUBE_API_KEY
    }
    r = requests.get(url, params=params, timeout=10)
    if r.status_code != 200:
        return None
    data = r.json()
    if not data["items"]:
        return None
    return int(data["items"][0]["statistics"]["viewCount"])

# ---------------- TRACKING ----------------
async def track_video(video_id, guild_id, channel_id, force=False):
    views = get_views(video_id)
    if views is None:
        return

    c.execute("SELECT title, last_views FROM videos WHERE video_id=?", (video_id,))
    row = c.fetchone()
    if not row:
        return

    title, last_views = row
    delta = views - last_views

    c.execute("""
        UPDATE videos
        SET last_views=?, last_checked=?
        WHERE video_id=?
    """, (views, int(time.time()), video_id))
    conn.commit()

    channel = bot.get_channel(channel_id)
    if channel and (delta != 0 or force):
        await channel.send(
            f"📊 **{title}**\n"
            f"+{format_views(max(delta,0))} views\n"
            f"Total: **{format_views(views)}**"
        )

    # milestones
    c.execute("SELECT milestone, ping FROM milestones WHERE video_id=?", (video_id,))
    for milestone, ping in c.fetchall():
        if views >= milestone:
            c.execute("""
                SELECT 1 FROM reached_milestones
                WHERE video_id=? AND milestone=?
            """, (video_id, milestone))
            if c.fetchone():
                continue

            c.execute("""
                INSERT INTO reached_milestones VALUES (?, ?, ?)
            """, (video_id, milestone, int(time.time())))
            conn.commit()

            msg = (
                f"🎉 **MILESTONE REACHED!**\n"
                f"🎬 {title}\n"
                f"🏁 {format_views(milestone)} views"
            )
            if ping:
                msg += f"\n{ping}"
            if channel:
                await channel.send(msg)

# ---------------- SCHEDULERS ----------------
async def kst_scheduler():
    await bot.wait_until_ready()
    last_run = None

    while True:
        now = datetime.now(KST)
        key = (now.hour, now.minute)

        if key in [(0,0), (12,0), (17,0)] and key != last_run:
            last_run = key
            c.execute("SELECT video_id, guild_id, channel_id FROM videos")
            for v,g,ch in c.fetchall():
                await track_video(v,g,ch)

        await asyncio.sleep(30)

async def interval_scheduler():
    await bot.wait_until_ready()

    while True:
        now = int(time.time())
        c.execute("""
            SELECT v.video_id, v.guild_id, v.channel_id,
                   i.interval_hours, i.next_run
            FROM videos v
            JOIN intervals i ON v.video_id = i.video_id
        """)
        for v,g,ch,h,n in c.fetchall():
            if now >= n:
                await track_video(v,g,ch)
                c.execute("""
                    UPDATE intervals
                    SET next_run=?
                    WHERE video_id=?
                """, (now + h*3600, v))
                conn.commit()
        await asyncio.sleep(60)

# ---------------- COMMANDS ----------------
@bot.tree.command(name="addvideo")
async def addvideo(i: discord.Interaction, video_id: str, title: str):
    c.execute("""
        INSERT OR REPLACE INTO videos
        VALUES (?, ?, ?, ?, 0, 0)
    """, (video_id, title, i.guild_id, i.channel_id))
    conn.commit()
    await i.response.send_message(f"✅ Added **{title}**")

@bot.tree.command(name="removevideo")
async def removevideo(i: discord.Interaction, video_id: str):
    c.execute("DELETE FROM videos WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    conn.commit()
    await i.response.send_message("🗑️ Removed video")

@bot.tree.command(name="listvideos")
async def listvideos(i: discord.Interaction):
    c.execute("""
        SELECT title, video_id FROM videos
        WHERE guild_id=? AND channel_id=?
    """, (i.guild_id, i.channel_id))
    rows = c.fetchall()
    if not rows:
        await i.response.send_message("No videos here.")
        return
    msg = "\n".join(f"• **{t}** (`{v}`)" for t,v in rows)
    await i.response.send_message(msg)

@bot.tree.command(name="views")
async def views(i: discord.Interaction, video_id: str):
    v = get_views(video_id)
    if v is None:
        await i.response.send_message("Error fetching views.")
        return
    await i.response.send_message(f"{format_views(v)} views")

@bot.tree.command(name="forcecheck")
async def forcecheck(i: discord.Interaction, video_id: str):
    await track_video(video_id, i.guild_id, i.channel_id, force=True)
    await i.response.send_message("✅ Checked")

@bot.tree.command(name="setmilestone")
async def setmilestone(i: discord.Interaction, video_id: str, milestone: int, ping: str=None):
    c.execute("INSERT OR IGNORE INTO milestones VALUES (?,?,?)",
              (video_id, milestone, ping))
    conn.commit()
    await i.response.send_message("🏁 Milestone set")

@bot.tree.command(name="removemilestone")
async def removemilestone(i: discord.Interaction, video_id: str, milestone: int):
    c.execute("DELETE FROM milestones WHERE video_id=? AND milestone=?",
              (video_id, milestone))
    conn.commit()
    await i.response.send_message("❌ Milestone removed")

@bot.tree.command(name="setinterval")
async def setinterval(i: discord.Interaction, video_id: str, hours: int):
    c.execute("""
        INSERT OR REPLACE INTO intervals
        VALUES (?, ?, ?)
    """, (video_id, hours, int(time.time()) + hours*3600))
    conn.commit()
    await i.response.send_message("⏱️ Interval set")

@bot.tree.command(name="disableinterval")
async def disableinterval(i: discord.Interaction, video_id: str):
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    conn.commit()
    await i.response.send_message("⛔ Interval disabled")

# ---------------- READY ----------------
@bot.event
async def on_ready():
    await bot.tree.sync()
    print("Bot online")
    bot.loop.create_task(kst_scheduler())
    bot.loop.create_task(interval_scheduler())

bot.run(DISCORD_TOKEN)
