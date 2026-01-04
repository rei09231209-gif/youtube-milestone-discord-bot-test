# --------------------- IMPORTS ---------------------
import discord
from discord.ext import commands
from discord import app_commands
import requests
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread
import os
import time

# --------------------- BOT SETUP ---------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# --------------------- DATABASE SETUP ---------------------
conn = sqlite3.connect("yt_track.db")
c = conn.cursor()

# Videos table
c.execute("""
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    guild_id INTEGER,
    channel_id INTEGER,
    last_views INTEGER DEFAULT 0,
    last_checked INTEGER
)
""")

# Milestones table
c.execute("""
CREATE TABLE IF NOT EXISTS milestones (
    video_id TEXT,
    milestone INTEGER,
    ping TEXT,
    PRIMARY KEY(video_id, milestone)
)
""")

# Reached milestones table
c.execute("""
CREATE TABLE IF NOT EXISTS reached_milestones (
    video_id TEXT,
    milestone INTEGER,
    timestamp INTEGER,
    PRIMARY KEY(video_id, milestone)
)
""")

# Custom intervals table
c.execute("""
CREATE TABLE IF NOT EXISTS intervals (
    video_id TEXT PRIMARY KEY,
    interval_hours INTEGER,
    next_run INTEGER
)
""")

conn.commit()

# --------------------- UTILITY FUNCTIONS ---------------------
def format_views(num):
    if num >= 1_000_000:
        return f"{num/1_000_000:.1f}M"
    elif num >= 1_000:
        return f"{num/1_000:.1f}K"
    else:
        return str(num)

def extract_video_id(url):
    if "watch?v=" in url:
        return url.split("watch?v=")[1].split("&")[0]
    elif "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0]
    return None

def get_views(video_id):
    try:
        response = requests.get(
            f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={video_id}&key={os.getenv('YOUTUBE_API_KEY')}"
        ).json()
        return int(response["items"][0]["statistics"]["viewCount"])
    except Exception:
        return None

# --------------------- MILESTONE LOGIC ---------------------
def check_milestones(video_id, new_views, channel):
    """
    Checks if any milestone is reached for a video.
    Posts message once per milestone.
    """
    c.execute("SELECT milestone, ping FROM milestones WHERE video_id=?", (video_id,))
    milestones = c.fetchall()
    for milestone, ping in milestones:
        if new_views >= milestone:
            # Prevent duplicate announcement
            c.execute(
                "SELECT 1 FROM reached_milestones WHERE video_id=? AND milestone=?",
                (video_id, milestone)
            )
            if c.fetchone():
                continue  # Already announced
            c.execute(
                "INSERT INTO reached_milestones VALUES (?,?,?)",
                (video_id, milestone, int(time.time()))
            )
            conn.commit()
            msg = f"🎉 **MILESTONE REACHED!**\n🎬 `{video_id}`\n🏁 {format_views(milestone)} views"
            if ping:
                msg += f"\n{ping}"
            # Send asynchronously
            asyncio.create_task(channel.send(msg))

# --------------------- UPCOMING MILESTONES SUMMARY ---------------------
async def upcoming_milestone_summary(guild_id, announcement_channel):
    """
    Sends upcoming milestones for all videos in the server.
    Only shows videos less than 100k away from next milestone.
    Sends one message per video, single ping at the end.
    """
    c.execute("""
        SELECT v.video_id, v.channel_id, v.last_views, m.milestone, m.ping
        FROM videos v
        JOIN milestones m ON v.video_id = m.video_id
        WHERE v.guild_id = ?
    """, (guild_id,))
    rows = c.fetchall()
    ping_roles = set()
    for video_id, channel_id, last_views, milestone, ping in rows:
        if last_views is None:
            last_views = 0
        remaining = milestone - last_views
        if 0 < remaining <= 100_000:  # Less than 100k away
            channel = bot.get_channel(channel_id)
            if channel:
                msg = f"⏳ **Upcoming Milestone**\n🎬 `{video_id}`\n📈 Next: {format_views(milestone)} views ({remaining} away)"
                await channel.send(msg)
                if ping:
                    ping_roles.add(ping)
    # Single ping after all messages
    if ping_roles:
        ping_msg = " ".join(ping_roles)
        await announcement_channel.send(ping_msg)

# --------------------- VIDEO TRACKING FUNCTION ---------------------
async def track_video(video_id, guild_id, channel_id):
    """
    Checks current views, updates database, and triggers milestones.
    """
    views = get_views(video_id)
    if views is None:
        return  # Skip if API failed
    c.execute(
        "SELECT last_views FROM videos WHERE video_id=?",
        (video_id,)
    )
    row = c.fetchone()
    last_views = row[0] if row else 0
    # Update database
    c.execute("""
        INSERT OR REPLACE INTO videos (video_id, guild_id, channel_id, last_views, last_checked)
        VALUES (?, ?, ?, ?, ?)
    """, (video_id, guild_id, channel_id, views, int(time.time())))
    conn.commit()
    # Fetch Discord channel
    channel = bot.get_channel(channel_id)
    if channel:
        # Post net increase since last check
        increase = views - last_views
        if increase != 0:
            await channel.send(f"📊 `{video_id}` updated. +{format_views(increase)} views (Total: {format_views(views)})")
        # Check milestones
        check_milestones(video_id, views, channel)

# --------------------- TIMEZONE SETUP ---------------------
KST = timezone(timedelta(hours=9))

# --------------------- VIDEO TRACKING SCHEDULER ---------------------
async def kst_tracking_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(KST)
        hour = now.hour
        minute = now.minute

        # Check for 12 AM, 12 PM, 5 PM KST
        if (hour == 0 or hour == 12 or hour == 17) and minute == 0:
            # Track all videos in all guilds
            c.execute("SELECT video_id, guild_id, channel_id FROM videos")
            videos = c.fetchall()
            for video_id, guild_id, channel_id in videos:
                asyncio.create_task(track_video(video_id, guild_id, channel_id))
            # Send upcoming milestone summary to a designated channel per guild
            c.execute("SELECT DISTINCT guild_id FROM videos")
            guilds = c.fetchall()
            for (guild_id,) in guilds:
                # Replace CHANNEL_ID with your announcement channel or fetch per guild from DB
                announcement_channel = bot.get_channel(CHANNEL_ID)
                if announcement_channel:
                    asyncio.create_task(upcoming_milestone_summary(guild_id, announcement_channel))
            # Wait 61 seconds to prevent double-trigger in same minute
            await asyncio.sleep(61)
        await asyncio.sleep(20)  # check every 20 seconds

# --------------------- CUSTOM INTERVAL LOOP ---------------------
async def custom_interval_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now_ts = int(time.time())
        c.execute("SELECT video_id, guild_id, channel_id, interval_hours, next_run FROM videos v JOIN intervals i ON v.video_id=i.video_id")
        rows = c.fetchall()
        for video_id, guild_id, channel_id, interval_hours, next_run in rows:
            if next_run is None:
                next_run = 0
            if now_ts >= next_run:
                asyncio.create_task(track_video(video_id, guild_id, channel_id))
                # Schedule next run
                next_run = now_ts + interval_hours * 3600
                c.execute("UPDATE intervals SET next_run=? WHERE video_id=?", (next_run, video_id))
                conn.commit()
        await asyncio.sleep(60)  # check every 60 seconds

# --------------------- KEEP ALIVE (OPEN PORT FOR RENDER) ---------------------
from flask import Flask
import threading

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_web():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_web, daemon=True).start()

# --------------------- SLASH COMMANDS ---------------------
@bot.tree.command(name="addvideo", description="Add a video to track in this channel")
async def addvideo(interaction: discord.Interaction, video_id: str):
    c.execute("""
        INSERT OR IGNORE INTO videos
        (video_id, guild_id, channel_id, last_views, last_checked)
        VALUES (?, ?, ?, 0, 0)
    """, (video_id, interaction.guild_id, interaction.channel_id))
    conn.commit()
    await interaction.response.send_message(f"✅ Added `{video_id}` for tracking in this channel.")

@bot.tree.command(name="removevideo", description="Remove a tracked video")
async def removevideo(interaction: discord.Interaction, video_id: str):
    c.execute("DELETE FROM videos WHERE video_id=? AND guild_id=?", (video_id, interaction.guild_id))
    conn.commit()
    await interaction.response.send_message(f"🗑️ Removed `{video_id}`.")

@bot.tree.command(name="listvideos", description="List videos tracked in this channel")
async def listvideos(interaction: discord.Interaction):
    c.execute("""
        SELECT video_id FROM videos
        WHERE guild_id=? AND channel_id=?
    """, (interaction.guild_id, interaction.channel_id))
    vids = c.fetchall()
    if not vids:
        await interaction.response.send_message("No videos tracked here.")
        return
    msg = "\n".join(f"• `{v[0]}`" for v in vids)
    await interaction.response.send_message(f"📋 **Tracked Videos:**\n{msg}")

@bot.tree.command(name="views", description="Get current views of a video")
async def views(interaction: discord.Interaction, video_id: str):
    views = get_views(video_id)
    if views is None:
        await interaction.response.send_message("❌ Failed to fetch views.")
        return
    await interaction.response.send_message(f"👁️ `{video_id}` → **{format_views(views)} views**")

@bot.tree.command(name="viewsall", description="Show current views for all videos in the server")
async def viewsall(interaction: discord.Interaction):
    await interaction.response.defer()
    c.execute("SELECT video_id FROM videos WHERE guild_id=?", (interaction.guild_id,))
    rows = c.fetchall()
    if not rows:
        await interaction.followup.send("No videos tracked in this server.")
        return
    for (vid,) in rows:
        v = get_views(vid)
        if v is not None:
            await interaction.followup.send(f"🎬 `{vid}` → {format_views(v)} views")

@bot.tree.command(name="forcecheck", description="Force check videos in this channel")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()
    c.execute("""
        SELECT video_id FROM videos
        WHERE guild_id=? AND channel_id=?
    """, (interaction.guild_id, interaction.channel_id))
    rows = c.fetchall()
    if not rows:
        await interaction.followup.send("No videos tracked in this channel.")
        return
    for (vid,) in rows:
        await track_video(vid, interaction.guild_id, interaction.channel_id)
    await interaction.followup.send("✅ Force check completed.")

# --------------------- MILESTONE COMMANDS ---------------------
@bot.tree.command(name="setmilestone", description="Set milestone for a video")
async def setmilestone(interaction: discord.Interaction, video_id: str, milestone: int, ping: str = None):
    c.execute("""
        INSERT INTO milestones (video_id, milestone, ping)
        VALUES (?, ?, ?)
    """, (video_id, milestone, ping))
    conn.commit()
    await interaction.response.send_message(f"🏁 Milestone `{format_views(milestone)}` set for `{video_id}`")

@bot.tree.command(name="removemilestone", description="Remove a milestone")
async def removemilestone(interaction: discord.Interaction, video_id: str, milestone: int):
    c.execute("""
        DELETE FROM milestones WHERE video_id=? AND milestone=?
    """, (video_id, milestone))
    conn.commit()
    await interaction.response.send_message("🗑️ Milestone removed.")

@bot.tree.command(name="listmilestones", description="List milestones of a video")
async def listmilestones(interaction: discord.Interaction, video_id: str):
    c.execute("SELECT milestone FROM milestones WHERE video_id=?", (video_id,))
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("No milestones.")
        return
    msg = "\n".join(format_views(r[0]) for r in rows)
    await interaction.response.send_message(f"🏁 **Milestones:**\n{msg}")

# --------------------- CUSTOM INTERVAL COMMANDS ---------------------
@bot.tree.command(name="setinterval", description="Set custom interval (hours) for a video")
async def setinterval(interaction: discord.Interaction, video_id: str, hours: int):
    c.execute("""
        INSERT OR REPLACE INTO intervals (video_id, interval_hours, next_run)
        VALUES (?, ?, ?)
    """, (video_id, hours, int(time.time()) + hours * 3600))
    conn.commit()
    await interaction.response.send_message(f"⏱️ Interval set to {hours} hours.")

@bot.tree.command(name="disableinterval", description="Disable custom interval for a video")
async def disableinterval(interaction: discord.Interaction, video_id: str):
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    conn.commit()
    await interaction.response.send_message("❌ Custom interval disabled.")

# --------------------- BOT READY ---------------------
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")
    bot.loop.create_task(kst_tracking_loop())
    bot.loop.create_task(custom_interval_loop())

# --------------------- START BOT ---------------------
bot.run(os.getenv("DISCORD_BOT_TOKEN"))
