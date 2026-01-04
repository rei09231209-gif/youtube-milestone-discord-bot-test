# ─────────────────────────────
# IMPORTS
# ─────────────────────────────
import os
import sqlite3
from datetime import datetime, timedelta, timezone
import asyncio
import requests
from aiohttp import web

import discord
from discord import app_commands
from discord.ext import tasks, commands

# ─────────────────────────────
# CONSTANTS
# ─────────────────────────────
KST = timezone(timedelta(hours=9))
TRACK_HOURS = [0, 12, 17]  # 12 AM, 12 PM, 5 PM KST
BOT_START_TIME = datetime.now(KST)
TRACK_LOG = {hour: None for hour in TRACK_HOURS}

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

# ─────────────────────────────
# BOT & INTENTS
# ─────────────────────────────
intents = discord.Intents.default()
intents.message_content = True  # required for slash commands
bot = commands.Bot(command_prefix="/", intents=intents)

# ─────────────────────────────
# DATABASE SETUP
# ─────────────────────────────
db = sqlite3.connect("bot_data.db")
c = db.cursor()

# Videos table
c.execute("""
CREATE TABLE IF NOT EXISTS videos(
    video_id TEXT PRIMARY KEY,
    title TEXT,
    guild_id INTEGER,
    channel_id INTEGER,
    last_views INTEGER
)
""")

# Milestones table
c.execute("""
CREATE TABLE IF NOT EXISTS milestones(
    video_id TEXT,
    milestone INTEGER,
    ping TEXT,
    PRIMARY KEY(video_id, milestone)
)
""")

# Milestone log
c.execute("""
CREATE TABLE IF NOT EXISTS milestone_log(
    video_id TEXT,
    milestone INTEGER,
    reached_at TEXT
)
""")

# Custom intervals table
c.execute("""
CREATE TABLE IF NOT EXISTS intervals(
    video_id TEXT PRIMARY KEY,
    hours INTEGER,
    next_run TEXT
)
""")

# Upcoming milestones alert setup per guild
c.execute("""
CREATE TABLE IF NOT EXISTS upcoming_alerts(
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    ping TEXT
)
""")

db.commit()

# ─────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────
def fmt(num):
    """Format number with commas."""
    return f"{num:,}"

def fetch_views(video_id: str):
    """Fetch YouTube video views using API."""
    if not YOUTUBE_API_KEY:
        print("⚠️ YOUTUBE_API_KEY not set")
        return None
    try:
        url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
        r = requests.get(url, timeout=10)
        data = r.json()
        if "items" not in data or not data["items"]:
            return None
        return int(data["items"][0]["statistics"]["viewCount"])
    except Exception as e:
        print(f"⚠️ fetch_views error for {video_id}: {e}")
        return None

# ─────────────────────────────
# KEEP-ALIVE SERVER (AIOHTTP)
# ─────────────────────────────
async def handle(request):
    return web.Response(text="Bot is alive!")

app = web.Application()
app.add_routes([web.get("/", handle)])

def start_keep_alive():
    """Start aiohttp server on port 8080 for Render."""
    loop = asyncio.get_event_loop()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
    loop.run_until_complete(site.start())
    print("🌐 Keep-alive server running on port", os.getenv("PORT", 8080))

# Start keep-alive before bot.run()
start_keep_alive()

# ─────────────────────────────
# /ADDVIDEO COMMAND
# ─────────────────────────────
@bot.tree.command(name="addvideo")
@app_commands.describe(video_id="YouTube video ID", title="Video title", channel="Discord channel to post updates")
async def addvideo(interaction: discord.Interaction, video_id: str, title: str, channel: discord.TextChannel):
    """Adds a video to track in the current channel."""
    views = fetch_views(video_id)
    if views is None:
        await interaction.response.send_message(f"❌ Could not fetch views for {video_id}.")
        return

    c.execute("""
    INSERT OR REPLACE INTO videos(video_id, title, guild_id, channel_id, last_views)
    VALUES (?,?,?,?,?)
    """, (video_id, title, interaction.guild.id, channel.id, views))
    db.commit()
    await interaction.response.send_message(f"✅ Video **{title}** added with current views {fmt(views)}.")

# ─────────────────────────────
# /REMOVEVIDEO COMMAND
# ─────────────────────────────
@bot.tree.command(name="removevideo")
@app_commands.describe(video_id="YouTube video ID")
async def removevideo(interaction: discord.Interaction, video_id: str):
    """Removes a video from tracking."""
    c.execute("DELETE FROM videos WHERE video_id=? AND guild_id=?", (video_id, interaction.guild.id))
    c.execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    db.commit()
    await interaction.response.send_message(f"🗑️ Video {video_id} removed from tracking.")

# ─────────────────────────────
# /LISTVIDEOS COMMAND
# ─────────────────────────────
@bot.tree.command(name="listvideos")
async def listvideos(interaction: discord.Interaction):
    """List all videos tracked in this channel/guild."""
    c.execute("SELECT title, video_id, last_views FROM videos WHERE guild_id=?", (interaction.guild.id,))
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("📭 No videos tracked in this server.")
        return

    msg = "**📹 Tracked Videos:**\n"
    for title, vid, views in rows:
        msg += f"- {title} ({vid}) | {fmt(views)} views\n"

    await interaction.response.send_message(msg)

# ─────────────────────────────
# /SERVERLIST COMMAND
# ─────────────────────────────
@bot.tree.command(name="serverlist")
async def serverlist(interaction: discord.Interaction):
    """List all videos tracked server-wide."""
    c.execute("SELECT title, video_id, last_views, channel_id FROM videos WHERE guild_id=?", (interaction.guild.id,))
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("📭 No videos tracked in this server.")
        return

    msg = "**🌐 Server Tracked Videos:**\n"
    for title, vid, views, ch_id in rows:
        msg += f"- {title} ({vid}) in <#{ch_id}> | {fmt(views)} views\n"
    await interaction.response.send_message(msg)

# ─────────────────────────────
# /VIEWSALL COMMAND
# ─────────────────────────────
@bot.tree.command(name="viewsall")
async def viewsall(interaction: discord.Interaction):
    """Run tracker for all videos in the server and report views."""
    c.execute("SELECT title, video_id, last_views, channel_id FROM videos WHERE guild_id=?", (interaction.guild.id,))
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("📭 No videos tracked in this server.")
        return

    for title, vid, last_views, ch_id in rows:
        views = fetch_views(vid)
        if views is None:
            continue
        diff = views - last_views
        c.execute("UPDATE videos SET last_views=? WHERE video_id=?", (views, vid))
        db.commit()
        channel = interaction.guild.get_channel(ch_id)
        if channel:
            await channel.send(f"📊 **Views Update:** {title} +{fmt(diff)} ({fmt(views)} total)")

    await interaction.response.send_message("✅ Viewsall tracker run completed.")

# ─────────────────────────────
# /SETMILESTONE COMMAND
# ─────────────────────────────
@bot.tree.command(name="setmilestone")
@app_commands.describe(video_id="YouTube video ID", milestone="Milestone in views", ping="Optional role/user mention")
async def setmilestone(interaction: discord.Interaction, video_id: str, milestone: int, ping: str = ""):
    """Set a milestone for a video with optional ping."""
    c.execute("INSERT OR REPLACE INTO milestones(video_id, milestone, ping) VALUES (?,?,?)",
              (video_id, milestone, ping))
    db.commit()
    await interaction.response.send_message(f"✅ Milestone {fmt(milestone)} set for video {video_id}.")

# ─────────────────────────────
# /REMOVEMILESTONE COMMAND
# ─────────────────────────────
@bot.tree.command(name="removemilestone")
@app_commands.describe(video_id="YouTube video ID", milestone="Milestone in views")
async def removemilestone(interaction: discord.Interaction, video_id: str, milestone: int):
    """Remove a milestone from a video."""
    c.execute("DELETE FROM milestones WHERE video_id=? AND milestone=?", (video_id, milestone))
    db.commit()
    await interaction.response.send_message(f"🗑️ Milestone {fmt(milestone)} removed from video {video_id}.")

# ─────────────────────────────
# /REACHEDMILESTONES COMMAND
# ─────────────────────────────
@bot.tree.command(name="reachedmilestones")
async def reachedmilestones(interaction: discord.Interaction):
    """Show milestones reached in the last 24 hours."""
    since = datetime.now(KST) - timedelta(days=1)
    c.execute("SELECT video_id, milestone, reached_at FROM milestone_log WHERE reached_at > ?", (since.isoformat(),))
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("📭 No milestones reached in the past 24 hours.")
        return

    msg = "**🏆 Reached Milestones (Last 24h):**\n"
    for vid, milestone, reached_at in rows:
        msg += f"- Video {vid} reached {fmt(milestone)} views at {reached_at}\n"
    await interaction.response.send_message(msg)

# ─────────────────────────────
# /UPCOMINGMILESTONES COMMAND
# ─────────────────────────────
@bot.tree.command(name="upcomingmilestones")
async def upcomingmilestones(interaction: discord.Interaction):
    """Show upcoming milestones less than 100k away from being reached."""
    c.execute("""
    SELECT v.title, v.video_id, v.last_views, m.milestone, m.ping
    FROM videos v
    JOIN milestones m ON v.video_id = m.video_id
    WHERE m.milestone - v.last_views <= 100_000
    """)
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("📭 No upcoming milestones within 100k views.")
        return

    msg = "**⏳ Upcoming Milestones (<100k away):**\n"
    for title, vid, views, milestone, ping in rows:
        msg += f"- {title} ({vid}): {fmt(views)}/{fmt(milestone)} views\n"
    await interaction.response.send_message(msg)

# ─────────────────────────────
# /SETUPCOMINGMILESTONESALERT COMMAND
# ─────────────────────────────
@bot.tree.command(name="setupcomingmilestonesalert")
@app_commands.describe(channel="Channel to post upcoming milestones", ping="Optional role/user ping")
async def setupcomingmilestonesalert(interaction: discord.Interaction, channel: discord.TextChannel, ping: str = ""):
    """Set the channel and ping for automatic upcoming milestones summary."""
    c.execute("INSERT OR REPLACE INTO upcoming_alerts(guild_id, channel_id, ping) VALUES (?,?,?)",
              (interaction.guild.id, channel.id, ping))
    db.commit()
    await interaction.response.send_message(f"✅ Upcoming milestones summary set for <#{channel.id}> with ping `{ping}`.")

# ─────────────────────────────
# KST TIME UTILS
# ─────────────────────────────
def now_kst():
    return datetime.now(KST)

# ─────────────────────────────
# TRACKING LOOP (12AM, 12PM, 5PM KST)
# ─────────────────────────────
@tasks.loop(minutes=5)
async def kst_tracker_loop():
    """Runs every 5 minutes and triggers tracking at 12AM, 12PM, 5PM KST."""
    current_hour = now_kst().hour
    if current_hour not in TRACK_HOURS:
        return

    # Prevent duplicate runs within the same hour
    if TRACK_LOG.get(current_hour) == now_kst().date():
        return

    TRACK_LOG[current_hour] = now_kst().date()

    c.execute("SELECT video_id, title, guild_id, channel_id, last_views FROM videos")
    rows = c.fetchall()

    for vid, title, guild_id, channel_id, last_views in rows:
        try:
            views = fetch_views(vid)
            if views is None:
                continue
            diff = views - last_views

            # Update video views
            c.execute("UPDATE videos SET last_views=? WHERE video_id=?", (views, vid))
            db.commit()

            # Send tracker update
            guild = bot.get_guild(guild_id)
            if guild:
                channel = guild.get_channel(channel_id)
                if channel:
                    await channel.send(f"⏰ **Scheduled Update:** {title} +{fmt(diff)} ({fmt(views)} total)")

            # Check milestones
            c.execute("SELECT milestone, ping FROM milestones WHERE video_id=?", (vid,))
            milestones = c.fetchall()
            for milestone, ping in milestones:
                if last_views < milestone <= views:
                    # Log milestone
                    c.execute("INSERT INTO milestone_log(video_id, milestone, reached_at) VALUES (?,?,?)",
                              (vid, milestone, now_kst().isoformat()))
                    db.commit()
                    # Send milestone alert
                    if guild and channel:
                        await channel.send(f"🏆 **Milestone Reached:** {title} reached {fmt(milestone)} views! {ping}")

        except Exception as e:
            print(f"⚠️ Error tracking video {vid}: {e}")

    # Run upcoming milestones summary automatically
    await upcoming_milestones_summary()

# ─────────────────────────────
# CUSTOM INTERVAL LOOP
# ─────────────────────────────
@tasks.loop(minutes=5)
async def custom_interval_loop():
    """Check all videos with custom intervals."""
    c.execute("""
    SELECT v.video_id, v.title, v.guild_id, v.channel_id, v.last_views, i.hours, i.next_run
    FROM intervals i
    JOIN videos v ON i.video_id=v.video_id
    """)
    rows = c.fetchall()

    for vid, title, guild_id, channel_id, last_views, hours, next_run in rows:
        try:
            next_run_dt = datetime.fromisoformat(next_run) if next_run else now_kst()
            if now_kst() < next_run_dt:
                continue

            # Fetch views
            views = fetch_views(vid)
            if views is None:
                continue
            diff = views - last_views

            # Update views
            c.execute("UPDATE videos SET last_views=? WHERE video_id=?", (views, vid))
            db.commit()

            # Send update
            guild = bot.get_guild(guild_id)
            if guild:
                channel = guild.get_channel(channel_id)
                if channel:
                    await channel.send(f"⏱️ **Custom Interval Update:** {title} +{fmt(diff)} ({fmt(views)} total)")

            # Schedule next run
            next_run_dt += timedelta(hours=hours)
            c.execute("UPDATE intervals SET next_run=? WHERE video_id=?", (next_run_dt.isoformat(), vid))
            db.commit()

        except Exception as e:
            print(f"⚠️ Error in custom_interval_loop for video {vid}: {e}")

# ─────────────────────────────
# UPCOMING MILESTONES SUMMARY
# ─────────────────────────────
async def upcoming_milestones_summary():
    """Automatic summary of upcoming milestones (<100k away) per server."""
    c.execute("SELECT guild_id, channel_id, ping FROM upcoming_alerts")
    alerts = c.fetchall()
    for guild_id, channel_id, ping_msg in alerts:
        try:
            c.execute("""
            SELECT v.title, v.video_id, v.last_views, m.milestone
            FROM videos v
            JOIN milestones m ON v.video_id=m.video_id
            WHERE v.guild_id=? AND m.milestone - v.last_views <= 100_000
            """, (guild_id,))
            rows = c.fetchall()
            if not rows:
                continue

            guild = bot.get_guild(guild_id)
            if not guild:
                continue
            channel = guild.get_channel(channel_id)
            if not channel:
                continue

            # Send one message per video
            for title, vid, views, milestone in rows:
                await channel.send(f"⏳ **Upcoming Milestone:** {title} ({vid}): {fmt(views)}/{fmt(milestone)} views")

            # Send a single ping at the end
            if ping_msg.strip():
                await channel.send(ping_msg)

        except Exception as e:
            print(f"⚠️ Error sending upcoming milestones summary for guild {guild_id}: {e}")

# ─────────────────────────────
# /SETINTERVAL COMMAND
# ─────────────────────────────
@bot.tree.command(name="setinterval")
@app_commands.describe(video_id="YouTube video ID", hours="Interval in hours")
async def setinterval(interaction: discord.Interaction, video_id: str, hours: int):
    """Set a custom interval tracking for a video (separate from 12am/12pm/5pm KST)."""
    next_run = now_kst() + timedelta(hours=hours)
    c.execute("INSERT OR REPLACE INTO intervals(video_id, hours, next_run) VALUES (?,?,?)",
              (video_id, hours, next_run.isoformat()))
    db.commit()
    await interaction.response.send_message(f"⏱️ Custom interval set for {video_id}: every {hours}h, next run at {next_run.strftime('%Y-%m-%d %H:%M:%S')} KST.")

# ─────────────────────────────
# /DISABLEINTERVAL COMMAND
# ─────────────────────────────
@bot.tree.command(name="disableinterval")
@app_commands.describe(video_id="YouTube video ID")
async def disableinterval(interaction: discord.Interaction, video_id: str):
    """Disable custom interval tracking for a video."""
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    db.commit()
    await interaction.response.send_message(f"🛑 Custom interval disabled for {video_id}.")

# ─────────────────────────────
# /BOTCHECK COMMAND
# ─────────────────────────────
@bot.tree.command(name="botcheck")
async def botcheck(interaction: discord.Interaction):
    """Check bot status: last tracker runs and upcoming intervals."""
    msg = "**🤖 Bot Status:**\n"
    for hour in TRACK_HOURS:
        last_run = TRACK_LOG.get(hour)
        last_run_str = last_run.strftime("%Y-%m-%d") if last_run else "Never"
        msg += f"- {hour}:00 KST tracker last run: {last_run_str}\n"

    # Show number of custom intervals
    c.execute("SELECT COUNT(*) FROM intervals")
    count = c.fetchone()[0]
    msg += f"- Custom interval tracking: {count} videos\n"

    await interaction.response.send_message(msg)

# ─────────────────────────────
# START BACKGROUND TASKS
# ─────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    try:
        await bot.tree.sync()
        print("🔗 Slash commands synced.")
    except Exception as e:
        print(f"⚠️ Error syncing commands: {e}")

    # Start loops
    if not kst_tracker_loop.is_running():
        kst_tracker_loop.start()
    if not custom_interval_loop.is_running():
        custom_interval_loop.start()

# ─────────────────────────────
# KEEP-ALIVE / RENDER PORT HANDLING
# ─────────────────────────────
from flask import Flask
from threading import Thread

app = Flask("")

@app.route("/")
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# Start Flask server in separate thread
t = Thread(target=run)
t.start()

# ─────────────────────────────
# ENVIRONMENT VARIABLES
# ─────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ ERROR: BOT_TOKEN environment variable not set!")
    exit(1)

# ─────────────────────────────
# RUN THE BOT
# ─────────────────────────────
bot.run(BOT_TOKEN)
