# ─────────────────────────────
# IMPORTS
# ─────────────────────────────
import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
import requests
import asyncio
from datetime import datetime, timedelta
import pytz
from flask import Flask
import threading
import os

# ─────────────────────────────
# KEEP ALIVE FOR RENDER
# ─────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_web():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_web, daemon=True).start()

# ─────────────────────────────
# BOT SETUP
# ─────────────────────────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="/", intents=intents)

# ─────────────────────────────
# TIMEZONE CONSTANTS
# ─────────────────────────────
KST = pytz.timezone("Asia/Seoul")

TRACK_HOURS = [0, 12, 17]  # 12 AM, 12 PM, 5 PM KST
TRACK_LOG = {0: None, 12: None, 17: None}  # Last run times
BOT_START_TIME = datetime.now(KST)

# ─────────────────────────────
# DATABASE SETUP
# ─────────────────────────────
db = sqlite3.connect("tracker.db", check_same_thread=False)
c = db.cursor()

# Videos table
c.execute("""
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    title TEXT,
    last_views INTEGER,
    guild_id INTEGER,
    channel_id INTEGER
)
""")

# Milestones table
c.execute("""
CREATE TABLE IF NOT EXISTS milestones (
    video_id TEXT,
    milestone INTEGER,
    ping TEXT
)
""")

# Milestone log table
c.execute("""
CREATE TABLE IF NOT EXISTS milestone_log (
    video_id TEXT,
    milestone INTEGER,
    reached_at TEXT
)
""")

# Custom intervals table
c.execute("""
CREATE TABLE IF NOT EXISTS intervals (
    video_id TEXT PRIMARY KEY,
    hours INTEGER,
    next_run TEXT
)
""")

db.commit()

# ─────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────
def fetch_views(video_id: str) -> int:
    """
    Fetch current views from YouTube public page.
    Returns integer views or None if failed.
    """
    try:
        r = requests.get(f"https://www.youtube.com/watch?v={video_id}", timeout=10)
        text = r.text
        idx = text.find("viewCount")
        digits = ""
        for ch in text[idx:idx+50]:
            if ch.isdigit():
                digits += ch
        return int(digits) if digits else None
    except:
        return None

def fmt(n: int) -> str:
    """
    Format large numbers for display
    """
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

# ─────────────────────────────
# SLASH COMMANDS — VIDEO TRACKING
# ─────────────────────────────
@bot.tree.command(name="addvideo")
@app_commands.describe(video_id="YouTube video ID", title="Video title")
async def addvideo(interaction: discord.Interaction, video_id: str, title: str):
    views = fetch_views(video_id)
    if views is None:
        await interaction.response.send_message("❌ Could not fetch video views.")
        return

    c.execute(
        "INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?)",
        (video_id, title, views, interaction.guild_id, interaction.channel_id)
    )
    db.commit()
    await interaction.response.send_message(f"✅ **{title}** added with {fmt(views)} views.")

@bot.tree.command(name="removevideo")
@app_commands.describe(video_id="YouTube video ID")
async def removevideo(interaction: discord.Interaction, video_id: str):
    c.execute(
        "DELETE FROM videos WHERE video_id=? AND channel_id=?",
        (video_id, interaction.channel_id)
    )
    db.commit()
    await interaction.response.send_message("🗑️ Video removed from tracking.")

@bot.tree.command(name="listvideos")
async def listvideos(interaction: discord.Interaction):
    c.execute(
        "SELECT title, last_views FROM videos WHERE channel_id=?",
        (interaction.channel_id,)
    )
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("No videos tracked here.")
        return

    msg = "**Tracked Videos (This Channel):**\n"
    for t,v in rows:
        msg += f"• {t} — {fmt(v)}\n"
    await interaction.response.send_message(msg)

@bot.tree.command(name="serverlist")
async def serverlist(interaction: discord.Interaction):
    c.execute(
        "SELECT title, last_views FROM videos WHERE guild_id=?",
        (interaction.guild_id,)
    )
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("No videos tracked in server.")
        return

    msg = "**Tracked Videos (Server):**\n"
    for t,v in rows:
        msg += f"• {t} — {fmt(v)}\n"
    await interaction.response.send_message(msg)

@bot.tree.command(name="forcecheck")
@app_commands.describe(video_id="YouTube video ID")
async def forcecheck(interaction: discord.Interaction, video_id: str):
    c.execute(
        "SELECT title, last_views FROM videos WHERE video_id=? AND channel_id=?",
        (video_id, interaction.channel_id)
    )
    row = c.fetchone()
    if not row:
        await interaction.response.send_message("❌ Video not tracked here.")
        return

    title, last_views = row
    views = fetch_views(video_id)
    if views is None:
        await interaction.response.send_message("❌ Failed to fetch views.")
        return

    diff = views - last_views
    c.execute(
        "UPDATE videos SET last_views=? WHERE video_id=?",
        (views, video_id)
    )
    db.commit()

    await interaction.response.send_message(
        f"📊 **{title}**\n+{fmt(diff)} ({fmt(views)} total)"
    )

# ─────────────────────────────
# SLASH COMMANDS — MILESTONES
# ─────────────────────────────
@bot.tree.command(name="setmilestone")
@app_commands.describe(video_id="YouTube video ID", milestone="Views to alert", ping="Role/User ping text")
async def setmilestone(interaction: discord.Interaction, video_id: str, milestone: int, ping: str):
    c.execute(
        "INSERT INTO milestones VALUES (?,?,?)",
        (video_id, milestone, ping)
    )
    db.commit()
    await interaction.response.send_message(f"✅ Milestone {fmt(milestone)} set for video {video_id}.")

@bot.tree.command(name="removemilestone")
@app_commands.describe(video_id="YouTube video ID", milestone="Milestone views to remove")
async def removemilestone(interaction: discord.Interaction, video_id: str, milestone: int):
    c.execute(
        "DELETE FROM milestones WHERE video_id=? AND milestone=?",
        (video_id, milestone)
    )
    db.commit()
    await interaction.response.send_message(f"🗑️ Milestone {fmt(milestone)} removed for video {video_id}.")

@bot.tree.command(name="listmilestones")
@app_commands.describe(video_id="YouTube video ID")
async def listmilestones(interaction: discord.Interaction, video_id: str):
    c.execute(
        "SELECT milestone FROM milestones WHERE video_id=?",
        (video_id,)
    )
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("No milestones set for this video.")
        return

    msg = f"**Milestones for {video_id}:**\n"
    for (m,) in rows:
        msg += f"• {fmt(m)}\n"
    await interaction.response.send_message(msg)

# ─────────────────────────────
# SLASH COMMANDS — UPCOMING & REACHED MILESTONES
# ─────────────────────────────
@bot.tree.command(name="upcomingmilestones")
async def upcomingmilestones(interaction: discord.Interaction):
    """
    Shows upcoming milestones within 100k views of being reached.
    Only one message per video, ping once at end.
    """
    c.execute("SELECT video_id, title, last_views FROM videos WHERE guild_id=?", (interaction.guild_id,))
    videos = c.fetchall()
    if not videos:
        await interaction.response.send_message("No videos tracked in server.")
        return

    messages = []
    pings = []
    for vid, title, views in videos:
        c.execute("SELECT milestone, ping FROM milestones WHERE video_id=?", (vid,))
        for m, ping in c.fetchall():
            if 0 < m - views <= 100_000:
                messages.append(f"• **{title}**: {fmt(views)} / {fmt(m)}")
                if ping:
                    pings.append(ping)

    if not messages:
        await interaction.response.send_message("No upcoming milestones within 100k views.")
        return

    full_msg = "**📈 Upcoming Milestones Summary:**\n" + "\n".join(messages)
    if pings:
        full_msg += "\n\n" + " ".join(set(pings))  # ping only once per unique role/user

    await interaction.response.send_message(full_msg)

@bot.tree.command(name="reachedmilestones")
async def reachedmilestones(interaction: discord.Interaction):
    """
    Shows milestones reached in the past 24 hours.
    """
    now = datetime.now(KST)
    since = now - timedelta(hours=24)
    c.execute("SELECT video_id, milestone, reached_at FROM milestone_log WHERE reached_at > ?", (since.isoformat(),))
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("No milestones reached in past 24 hours.")
        return

    msg = "**🏆 Reached Milestones (Past 24h):**\n"
    for vid, m, t in rows:
        msg += f"• {vid}: {fmt(m)} at {t}\n"
    await interaction.response.send_message(msg)

# ─────────────────────────────
# SLASH COMMAND — BOT CHECK
# ─────────────────────────────
@bot.tree.command(name="botcheck")
async def botcheck(interaction: discord.Interaction):
    now = datetime.now(KST)
    uptime = now - BOT_START_TIME

    def status(hour):
        last = TRACK_LOG.get(hour)
        if last is None:
            return "❌ Not yet"
        if last.date() == now.date():
            return "✅ Done"
        return "⏳ Pending"

    c.execute("SELECT COUNT(*) FROM videos WHERE guild_id=?", (interaction.guild_id,))
    count = c.fetchone()[0]

    msg = (
        "🤖 **Bot Status Check**\n\n"
        f"• 12 AM KST tracking: {status(0)}\n"
        f"• 12 PM KST tracking: {status(12)}\n"
        f"• 5 PM KST tracking: {status(17)}\n\n"
        f"• Videos tracked in server: {count}\n"
        f"• Bot uptime: {str(uptime).split('.')[0]}"
    )
    await interaction.response.send_message(msg)

# ─────────────────────────────
# TRACKING SCHEDULER (CLOCKWISE KST)
# ─────────────────────────────
@tasks.loop(seconds=60)
async def kst_scheduler():
    now = datetime.now(KST)
    hour_min = (now.hour, now.minute)
    for hour in TRACK_HOURS:
        if now.hour == hour and TRACK_LOG[hour] != now.date():
            # Update last run log
            TRACK_LOG[hour] = now
            await run_tracking(hour)

async def run_tracking(hour):
    """
    Loop over videos and update views, check milestones.
    """
    c.execute("SELECT video_id, title, last_views, guild_id, channel_id FROM videos")
    videos = c.fetchall()
    for vid, title, last_views, guild_id, channel_id in videos:
        views = fetch_views(vid)
        if views is None:
            continue

        diff = views - last_views
        c.execute("UPDATE videos SET last_views=? WHERE video_id=?", (views, vid))
        db.commit()

        # Check milestones
        c.execute("SELECT milestone, ping FROM milestones WHERE video_id=?", (vid,))
        for m, ping in c.fetchall():
            if last_views < m <= views:
                # Send alert
                channel = bot.get_guild(guild_id).get_channel(channel_id)
                if channel:
                    msg = f"🏆 **Milestone reached!** {title} hit {fmt(m)} views!"
                    if ping:
                        msg += f" {ping}"
                    await channel.send(msg)
                # Log milestone
                c.execute(
                    "INSERT INTO milestone_log VALUES (?,?,?)",
                    (vid, m, datetime.now(KST).isoformat())
                )
                db.commit()

# ─────────────────────────────
# CUSTOM INTERVAL COMMANDS
# ─────────────────────────────
@bot.tree.command(name="setinterval")
@app_commands.describe(video_id="YouTube video ID", hours="Interval in hours")
async def setinterval(interaction: discord.Interaction, video_id: str, hours: int):
    """
    Sets a custom interval (in hours) for a video.
    """
    next_run = datetime.now(KST) + timedelta(hours=hours)
    c.execute(
        "INSERT OR REPLACE INTO intervals VALUES (?,?,?)",
        (video_id, hours, next_run.isoformat())
    )
    db.commit()
    await interaction.response.send_message(f"⏱️ Custom interval of {hours}h set for video {video_id}.")

@bot.tree.command(name="disableinterval")
@app_commands.describe(video_id="YouTube video ID")
async def disableinterval(interaction: discord.Interaction, video_id: str):
    """
    Disables custom interval for a video.
    """
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    db.commit()
    await interaction.response.send_message(f"⏹️ Custom interval disabled for video {video_id}.")

# ─────────────────────────────
# CUSTOM INTERVAL LOOP
# ─────────────────────────────
@tasks.loop(minutes=1)
async def custom_interval_loop():
    """
    Checks if any video with a custom interval is due to run.
    """
    now = datetime.now(KST)
    c.execute("SELECT video_id, guild_id, channel_id, hours, next_run FROM intervals i JOIN videos v ON i.video_id=v.video_id")
    rows = c.fetchall()
    for vid, guild_id, channel_id, hours, next_run in rows:
        next_run_dt = datetime.fromisoformat(next_run)
        if now >= next_run_dt:
            # Run update for this video only
            c.execute("SELECT title, last_views FROM videos WHERE video_id=?", (vid,))
            row = c.fetchone()
            if not row:
                continue
            title, last_views = row
            views = fetch_views(vid)
            if views is None:
                continue
            diff = views - last_views
            c.execute("UPDATE videos SET last_views=? WHERE video_id=?", (views, vid))
            db.commit()

            # Send message
            guild = bot.get_guild(guild_id)
            if guild:
                channel = guild.get_channel(channel_id)
                if channel:
                    await channel.send(f"⏱️ **Custom Interval Update:** {title} +{fmt(diff)} ({fmt(views)} total)")

            # Update next_run
            next_run_dt += timedelta(hours=hours)
            c.execute("UPDATE intervals SET next_run=? WHERE video_id=?", (next_run_dt.isoformat(), vid))
            db.commit()

# ─────────────────────────────
# START ALL LOOPS AND BOT
# ─────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} | ID: {bot.user.id}")
    try:
        synced = await bot.tree.sync()
        print(f"🌐 Slash commands synced: {len(synced)} commands.")
    except Exception as e:
        print(f"⚠️ Failed to sync commands: {e}")

    if not kst_scheduler.is_running():
        kst_scheduler.start()
    if not custom_interval_loop.is_running():
        custom_interval_loop.start()

# ─────────────────────────────
# BOT TOKEN (ENV VARIABLE)
# ─────────────────────────────
# Make sure you set your bot token as an environment variable named BOT_TOKEN
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    print("❌ Error: BOT_TOKEN environment variable not set.")
    exit(1)

# ─────────────────────────────
# RUN BOT
# ─────────────────────────────
bot.run(TOKEN)
