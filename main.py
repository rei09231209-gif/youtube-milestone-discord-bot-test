import os
import sqlite3
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
from datetime import datetime, timedelta
from flask import Flask
import threading
import asyncio

# =========================
# ENVIRONMENT VARIABLES
# =========================

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

if not BOT_TOKEN:
    print("❌ ERROR: DISCORD_BOT_TOKEN missing.")
if not YOUTUBE_API_KEY:
    print("❌ ERROR: YOUTUBE_API_KEY missing.")

# =========================
# GLOBAL HTTP SESSION
# =========================
session: aiohttp.ClientSession | None = None

async def create_session():
    global session
    if session is None or session.closed:
        session = aiohttp.ClientSession()

# =========================
# DATABASE
# =========================

db = sqlite3.connect("db.sqlite", check_same_thread=False)
c = db.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    title TEXT,
    guild_id INTEGER,
    channel_id INTEGER,
    alert_channel INTEGER
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS milestones (
    video_id TEXT PRIMARY KEY,
    last_million INTEGER DEFAULT 0,
    ping TEXT DEFAULT ''
)
""")

# FIXED → restored 5-column table
c.execute("""
CREATE TABLE IF NOT EXISTS intervals (
    video_id TEXT PRIMARY KEY,
    hours REAL,
    next_time TEXT,
    last_views INTEGER,
    temp_views INTEGER
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS upcoming_alerts (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    ping TEXT
)
""")

db.commit()

# =========================
# TIME UTILS
# =========================

def now_kst():
    return datetime.utcnow() + timedelta(hours=9)

# =========================
# YOUTUBE FETCH
# =========================

async def fetch_views(video_id):
    """Global-session YouTube fetch with retries."""
    await create_session()

    params = {
        "part": "statistics",
        "id": video_id,
        "key": YOUTUBE_API_KEY
    }

    for _ in range(3):  # retry up to 3 times
        try:
            async with session.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params=params,
                timeout=10
            ) as r:

                if r.status != 200:
                    await asyncio.sleep(1)
                    continue

                data = await r.json()
                if not data.get("items"):
                    return None

                return int(data["items"][0]["statistics"]["viewCount"])

        except Exception:
            await asyncio.sleep(1)

    return None

# =========================
# DISCORD BOT
# =========================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# =========================
# SLASH COMMANDS
# =========================

@tree.command(description="Add a video to track")
async def addvideo(interaction: discord.Interaction, video_id: str, title: str):
    alert_channel = interaction.channel.id

    # FIX: insert 5 columns into intervals
    c.execute("INSERT OR IGNORE INTO videos VALUES (?,?,?,?,?)",
              (video_id, title, interaction.guild.id, interaction.channel.id, alert_channel))

    c.execute("INSERT OR IGNORE INTO milestones VALUES (?,?,?)",
              (video_id, 0, ""))

    next_time = now_kst().isoformat()
    c.execute("INSERT OR IGNORE INTO intervals VALUES (?,?,?,?,?)",
              (video_id, 0, next_time, 0, 0))

    db.commit()

    await interaction.response.send_message(
        f"✅ Tracking **{title}**\n📌 Updates in <#{alert_channel}>"
    )


@tree.command(description="Remove a tracked video")
async def removevideo(interaction: discord.Interaction, video_id: str):
    c.execute("DELETE FROM videos WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    db.commit()
    await interaction.response.send_message("🗑️ Removed.")


@tree.command(description="List videos in this channel")
async def listvideos(interaction: discord.Interaction):
    c.execute("SELECT title FROM videos WHERE channel_id=?", (interaction.channel.id,))
    rows = c.fetchall()
    if not rows:
        return await interaction.response.send_message("📭 No videos.")
    await interaction.response.send_message("\n".join(f"• {r[0]}" for r in rows))


@tree.command(description="List all server videos")
async def serverlist(interaction: discord.Interaction):
    c.execute("SELECT title FROM videos WHERE guild_id=?", (interaction.guild.id,))
    rows = c.fetchall()
    if not rows:
        return await interaction.response.send_message("📭 No videos in server.")
    await interaction.response.send_message("\n".join(f"• {r[0]}" for r in rows))


@tree.command(description="Force check videos in this channel")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()

    c.execute("SELECT title, video_id FROM videos WHERE channel_id=?", (interaction.channel.id,))
    videos = c.fetchall()

    if not videos:
        return await interaction.followup.send("⚠️ None tracked.")

    for title, vid in videos:
        views = await fetch_views(vid)
        if views is None:
            await interaction.followup.send(f"❌ Error fetching **{title}**")
            continue

        c.execute("SELECT last_views FROM intervals WHERE video_id=?", (vid,))
        row = c.fetchone()
        old = row[0] if row else 0
        net = views - old

        c.execute("UPDATE intervals SET last_views=? WHERE video_id=?", (views, vid))
        db.commit()

        await interaction.followup.send(
            f"📊 **{title}** — {views:,} views (+{net:,})"
        )


@tree.command(description="Get current views")
async def views(interaction: discord.Interaction, video_id: str):
    v = await fetch_views(video_id)
    if v is None:
        return await interaction.response.send_message("❌ Error fetching views.")
    await interaction.response.send_message(f"📊 {v:,} views")


@tree.command(description="Show views for all tracked videos")
async def viewsall(interaction: discord.Interaction):
    await interaction.response.defer()

    c.execute("SELECT title, video_id FROM videos WHERE guild_id=?", (interaction.guild.id,))
    videos = c.fetchall()

    if not videos:
        return await interaction.followup.send("⚠️ None tracked.")

    for title, vid in videos:
        v = await fetch_views(vid)
        if v is None:
            await interaction.followup.send(f"❌ {title}: error")
            continue

        c.execute("SELECT last_views FROM intervals WHERE video_id=?", (vid,))
        row = c.fetchone()
        old = row[0] if row else 0
        net = v - old

        c.execute("UPDATE intervals SET last_views=? WHERE video_id=?", (v, vid))
        db.commit()

        await interaction.followup.send(
            f"📊 **{title}** — {v:,} (+{net:,})"
        )


@tree.command(description="Set milestone alert channel")
async def setmilestone(interaction: discord.Interaction, video_id: str, channel: discord.TextChannel, ping: str = ""):
    c.execute("UPDATE milestones SET ping=? WHERE video_id=?",
              (f"{channel.id}|{ping}", video_id))
    db.commit()
    await interaction.response.send_message(f"🏆 Alerts → <#{channel.id}>")


@tree.command(description="Show reached milestones")
async def reachedmilestones(interaction: discord.Interaction):
    await interaction.response.defer()

    c.execute("SELECT title, video_id FROM videos WHERE guild_id=?", (interaction.guild.id,))
    videos = c.fetchall()

    lines = []
    for title, vid in videos:
        c.execute("SELECT last_million FROM milestones WHERE video_id=?", (vid,))
        row = c.fetchone()
        if row and row[0] > 0:
            lines.append(f"🏆 **{title}** — {row[0]}M views")

    if not lines:
        return await interaction.followup.send("📭 None yet.")

    await interaction.followup.send("\n".join(lines))


@tree.command(description="Remove milestone alerts")
async def removemilestones(interaction: discord.Interaction, video_id: str):
    c.execute("UPDATE milestones SET ping='' WHERE video_id=?", (video_id,))
    db.commit()
    await interaction.response.send_message("❌ Removed.")


@tree.command(description="Set custom interval in hours")
async def setinterval(interaction: discord.Interaction, video_id: str, hours: float):
    next_time = now_kst() + timedelta(hours=hours)
    c.execute("INSERT OR REPLACE INTO intervals VALUES (?,?,?,?,?)",
              (video_id, hours, next_time.isoformat(), 0, 0))
    db.commit()
    await interaction.response.send_message("⏱️ Interval set.")


@tree.command(description="Disable interval tracking")
async def disableinterval(interaction: discord.Interaction, video_id: str):
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    db.commit()
    await interaction.response.send_message("❌ Disabled.")


@tree.command(description="Setup upcoming milestone summary")
async def setupcomingmilestonesalert(interaction: discord.Interaction, channel: discord.TextChannel, ping: str = ""):
    c.execute("INSERT OR REPLACE INTO upcoming_alerts VALUES (?,?,?)",
              (interaction.guild.id, channel.id, ping))
    db.commit()
    await interaction.response.send_message("📌 Upcoming summary set.")


@tree.command(description="Show upcoming milestones within 100k")
async def upcoming(interaction: discord.Interaction):
    await interaction.response.defer()

    c.execute("SELECT title, video_id FROM videos WHERE guild_id=?", (interaction.guild.id,))
    videos = c.fetchall()

    lines = []

    for title, vid in videos:
        v = await fetch_views(vid)
        if v is None:
            continue

        next_m = ((v // 1_000_000) + 1) * 1_000_000
        diff = next_m - v

        if diff <= 100_000:
            lines.append(f"⏳ **{title}** — {diff:,} away from {next_m:,}")

    if not lines:
        return await interaction.followup.send("📭 None.")

    await interaction.followup.send("\n".join(lines))


@tree.command(description="Bot health check")
async def botcheck(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"✅ OK — KST: {now_kst().strftime('%Y-%m-%d %H:%M')}"
    )

# =========================
# BACKGROUND TASKS
# =========================

@tasks.loop(minutes=1)
async def tracking_loop():
    now = now_kst()

    c.execute("SELECT video_id, hours, next_time, last_views, temp_views FROM intervals")
    rows = c.fetchall()

    for vid, hours, next_t, last, temp in rows:
        next_time = datetime.fromisoformat(next_t)

        if now >= next_time:
            views = await fetch_views(vid)
            if views is None:
                continue

            net = views - last if last else 0

            c.execute("UPDATE intervals SET last_views=?, next_time=? WHERE video_id=?",
                      (views, (now + timedelta(hours=hours)).isoformat(), vid))
            db.commit()

            c.execute("SELECT title, alert_channel FROM videos WHERE video_id=?", (vid,))
            row = c.fetchone()
            if row:
                title, ch = row
                channel = bot.get_channel(ch)
                if channel:
                    await channel.send(f"📈 **{title}** — {views:,} (+{net:,})")


@tasks.loop(minutes=2)
async def kst_tracker():
    now = now_kst()

    if now.hour != 0 or now.minute != 0:
        return

    # Daily milestones
    c.execute("SELECT video_id, title FROM videos")
    videos = c.fetchall()

    for vid, title in videos:
        views = await fetch_views(vid)
        if views is None:
            continue

        current_m = views // 1_000_000

        c.execute("SELECT last_million, ping FROM milestones WHERE video_id=?", (vid,))
        row = c.fetchone()

        if row and current_m > row[0]:
            if row[1]:
                ping_ch, ping = row[1].split("|")
                channel = bot.get_channel(int(ping_ch))
                if channel:
                    await channel.send(f"🏆 **{title}** reached **{current_m}M** {ping}")

            c.execute("UPDATE milestones SET last_million=? WHERE video_id=?", (current_m, vid))
            db.commit()

    # Upcoming summary
    c.execute("SELECT guild_id, channel_id, ping FROM upcoming_alerts")
    alerts = c.fetchall()

    for g_id, ch_id, ping in alerts:
        c.execute("SELECT title, video_id FROM videos WHERE guild_id=?", (g_id,))
        vids = c.fetchall()

        lines = []
        for title, vid in vids:
            v = await fetch_views(vid)
            if v is None:
                continue
            next_m = ((v // 1_000_000) + 1) * 1_000_000
            diff = next_m - v
            if diff <= 100_000:
                lines.append(f"⏳ {title} — {diff:,} away from {next_m:,}")

        if lines:
            ch = bot.get_channel(ch_id)
            if ch:
                await ch.send(
                    "📌 **Upcoming Milestones Today**\n" +
                    "\n".join(lines) +
                    (f"\n{ping}" if ping else "")
                )

# =========================
# BOT READY
# =========================

@bot.event
async def on_ready():
    print(f"🚀 Logged in as {bot.user}")
    await create_session()
    await tree.sync()
    tracking_loop.start()
    kst_tracker.start()
    print("⏱️ Tracking loops running.")

# =========================
# KEEPALIVE SERVER
# =========================

app = Flask("keepalive")

@app.route("/")
def home():
    return "OK - Bot alive"

def run_server():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_server).start()

# =========================
# RUN BOT
# =========================

bot.run(BOT_TOKEN)
