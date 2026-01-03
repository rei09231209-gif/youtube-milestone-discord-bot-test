import discord
from discord.ext import tasks
from discord import app_commands
import asyncio
import requests
import sqlite3
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask
import os
import time

# ---------------- ENVIRONMENT ----------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]

# ---------------- SQLITE SETUP ----------------
conn = sqlite3.connect("botdata.db")
c = conn.cursor()

# Videos table: track video per guild/channel
c.execute('''
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    guild_id TEXT,
    channel_id TEXT,
    title TEXT,
    last_views INTEGER,
    last_checked INTEGER
)
''')

# Milestones table: per video
c.execute('''
CREATE TABLE IF NOT EXISTS milestones (
    video_id TEXT,
    milestone INTEGER,
    ping TEXT,
    PRIMARY KEY(video_id, milestone)
)
''')

# Reached milestones (once per milestone)
c.execute('''
CREATE TABLE IF NOT EXISTS reached_milestones (
    video_id TEXT,
    milestone INTEGER,
    timestamp INTEGER
)
''')

# Custom intervals (optional per video)
c.execute('''
CREATE TABLE IF NOT EXISTS intervals (
    video_id TEXT,
    interval_hours INTEGER,
    last_run INTEGER
)
''')

conn.commit()

# ---------------- KEEP ALIVE ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!", 200

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run_web)
    t.daemon = True
    t.start()

# ---------------- DISCORD BOT SETUP ----------------
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ---------------- HELPER FUNCTIONS ----------------
def extract_video_id(url: str):
    if "v=" in url:
        return url.split("v=")[-1].split("&")[0]
    if "youtu.be/" in url:
        return url.split("youtu.be/")[-1].split("?")[0]
    return None


def fetch_views(video_id: str):
    try:
        url = (
            "https://www.googleapis.com/youtube/v3/videos"
            f"?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
        )
        r = requests.get(url, timeout=10)
        data = r.json()
        return int(data["items"][0]["statistics"]["viewCount"])
    except Exception:
        return None


def format_views(num: int):
    if num >= 1_000_000_000:
        return f"{num/1_000_000_000:.2f}B"
    if num >= 1_000_000:
        return f"{num/1_000_000:.2f}M"
    if num >= 1_000:
        return f"{num/1_000:.2f}K"
    return str(num)


def now_kst():
    return datetime.utcnow() + timedelta(hours=9)

# ---------------- TRACK TIME CONSTANTS ----------------
# These are the ONLY fixed tracking times
TRACK_TIMES_KST = ["00:00", "12:00", "17:00"]  # 12 AM, 12 PM, 5 PM KST

# ---------------- SCHEDULER ----------------
async def clock_scheduler():
    await bot.wait_until_ready()

    # keeps track of when each time last ran
    last_run = {t: None for t in TRACK_TIMES_KST}

    while not bot.is_closed():
        kst = now_kst()
        current_time = kst.strftime("%H:%M")

        if current_time in last_run:
            if last_run[current_time] != kst.date():
                last_run[current_time] = kst.date()
                await run_tracking_cycle(tag=current_time)

        await asyncio.sleep(60)

# ---------------- CORE TRACKING LOGIC ----------------
async def run_tracking_cycle(tag: str, guild_id=None, channel_id=None):
    """
    Main tracking cycle.
    - Runs at 12 AM / 12 PM / 5 PM KST automatically
    - Can be scoped via forcecheck (guild/channel)
    - Tracks NET increase only
    """

    query = "SELECT video_id, guild_id, channel_id, title, last_views FROM videos"
    params = []

    if guild_id and channel_id:
        query += " WHERE guild_id=? AND channel_id=?"
        params = [str(guild_id), str(channel_id)]
    elif guild_id:
        query += " WHERE guild_id=?"
        params = [str(guild_id)]

    c.execute(query, params)
    rows = c.fetchall()

    for video_id, g_id, ch_id, title, last_views in rows:
        current_views = fetch_views(video_id)
        if current_views is None:
            continue

        # Calculate net increase
        if last_views is None:
            net = 0
        else:
            net = current_views - last_views

        # Update database immediately (prevents double-counting on crashes)
        c.execute(
            "UPDATE videos SET last_views=?, last_checked=? WHERE video_id=?",
            (current_views, int(time.time()), video_id)
        )
        conn.commit()

        # Only send message if views increased
        if net <= 0:
            continue

        channel = bot.get_channel(int(ch_id))
        if not channel:
            continue

        await channel.send(
            f"📊 **Tracking Update ({tag} KST)**\n"
            f"🎬 **{title}**\n"
            f"📈 +{format_views(net)} → {format_views(current_views)}"
        )

        # Check milestones AFTER sending tracking update
        await check_milestones(video_id, current_views, channel)

async def check_milestones(video_id, views, channel):
    c.execute(
        "SELECT milestone, ping FROM milestones WHERE video_id=?",
        (video_id,)
    )
    milestones = c.fetchall()

    for milestone, ping in milestones:
        # Skip if already reached
        c.execute(
            "SELECT 1 FROM reached_milestones WHERE video_id=? AND milestone=?",
            (video_id, milestone)
        )
        if c.fetchone():
            continue

        if views >= milestone:
            message = (
                f"🎉 **MILESTONE REACHED!**\n"
                f"🎬 `{video_id}`\n"
                f"🏁 {format_views(milestone)}"
            )

            if ping:
                message += f"\n{ping}"

            await channel.send(message)

            c.execute(
                "INSERT INTO reached_milestones VALUES (?,?,?)",
                (video_id, milestone, int(time.time()))
            )
            conn.commit()

@tree.command(name="addvideo")
async def addvideo(
    interaction: discord.Interaction,
    url: str,
    title: str
):
    await interaction.response.defer()

    video_id = extract_video_id(url)
    if not video_id:
        await interaction.followup.send("❌ Invalid YouTube URL")
        return

    # Fetch initial views
    views = fetch_views(video_id)
    if views is None:
        await interaction.followup.send("❌ Could not fetch video views")
        return

    c.execute(
        "INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?,?)",
        (
            video_id,
            str(interaction.guild_id),
            str(interaction.channel_id),
            title,
            views,
            int(time.time())
        )
    )
    conn.commit()

    await interaction.followup.send(
        f"✅ **Video Added**\n"
        f"🎬 **{title}**\n"
        f"👁️ Starting at {format_views(views)} views"
    )

@tree.command(name="removevideo")
async def removevideo(interaction: discord.Interaction, url: str):
    video_id = extract_video_id(url)
    if not video_id:
        await interaction.response.send_message("❌ Invalid YouTube URL")
        return

    c.execute("DELETE FROM videos WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM reached_milestones WHERE video_id=?", (video_id,))
    conn.commit()

    await interaction.response.send_message("🗑️ Video removed from tracking")

@tree.command(name="listvideos")
async def listvideos(interaction: discord.Interaction):
    c.execute(
        "SELECT title FROM videos WHERE guild_id=? AND channel_id=?",
        (str(interaction.guild_id), str(interaction.channel_id))
    )
    rows = c.fetchall()

    if not rows:
        await interaction.response.send_message("📭 No videos tracked in this channel")
        return

    msg = "📂 **Tracked Videos (This Channel)**\n\n"
    for (title,) in rows:
        msg += f"• {title}\n"

    await interaction.response.send_message(msg)

@tree.command(name="serverlist")
async def serverlist(interaction: discord.Interaction):
    c.execute(
        "SELECT title, channel_id FROM videos WHERE guild_id=?",
        (str(interaction.guild_id),)
    )
    rows = c.fetchall()

    if not rows:
        await interaction.response.send_message("📭 No videos tracked in this server")
        return

    msg = "🗂️ **Server Video List**\n\n"
    for title, ch_id in rows:
        channel = bot.get_channel(int(ch_id))
        ch_name = channel.name if channel else "unknown"
        msg += f"🎬 **{title}** → #{ch_name}\n"

    await interaction.response.send_message(msg)

@tree.command(name="views")
async def views(interaction: discord.Interaction, url: str):
    video_id = extract_video_id(url)
    if not video_id:
        await interaction.response.send_message("❌ Invalid YouTube URL")
        return

    views = fetch_views(video_id)
    if views is None:
        await interaction.response.send_message("❌ Could not fetch views")
        return

    await interaction.response.send_message(
        f"👁️ **Current Views**\n"
        f"🎬 `{video_id}`\n"
        f"👀 {format_views(views)}"
    )

@tree.command(name="forcecheck")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()

    await run_tracking_cycle(
        tag="FORCECHECK",
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id
    )

    await interaction.followup.send("✅ Forcecheck complete for this channel")

@tree.command(name="setmilestone")
async def setmilestone(
    interaction: discord.Interaction,
    url: str,
    milestone: int,
    ping: str = ""
):
    video_id = extract_video_id(url)
    if not video_id:
        await interaction.response.send_message("❌ Invalid YouTube URL")
        return

    c.execute(
        "INSERT INTO milestones VALUES (?,?,?)",
        (video_id, milestone, ping)
    )
    conn.commit()

    await interaction.response.send_message(
        f"✅ **Milestone Set**\n"
        f"🎬 `{video_id}`\n"
        f"🎯 {format_views(milestone)} views"
    )

@tree.command(name="removemilestone")
async def removemilestone(
    interaction: discord.Interaction,
    url: str,
    milestone: int
):
    video_id = extract_video_id(url)
    if not video_id:
        await interaction.response.send_message("❌ Invalid URL")
        return

    c.execute(
        "DELETE FROM milestones WHERE video_id=? AND milestone=?",
        (video_id, milestone)
    )
    conn.commit()

    await interaction.response.send_message("🗑️ Milestone removed")

@tree.command(name="listmilestones")
async def listmilestones(interaction: discord.Interaction, url: str):
    video_id = extract_video_id(url)
    if not video_id:
        await interaction.response.send_message("❌ Invalid URL")
        return

    c.execute(
        "SELECT milestone FROM milestones WHERE video_id=? ORDER BY milestone",
        (video_id,)
    )
    rows = c.fetchall()

    if not rows:
        await interaction.response.send_message("📭 No milestones set")
        return

    msg = "🎯 **Milestones**\n\n"
    for (m,) in rows:
        msg += f"• {format_views(m)}\n"

    await interaction.response.send_message(msg)

@tree.command(name="reachedmilestones")
async def reachedmilestones(interaction: discord.Interaction):
    since = int(time.time()) - 86400

    c.execute(
        "SELECT video_id, milestone, reached_at FROM reached_milestones WHERE reached_at>=?",
        (since,)
    )
    rows = c.fetchall()

    if not rows:
        await interaction.response.send_message("📭 No milestones reached in last 24h")
        return

    msg = "🏁 **Reached Milestones (24h)**\n\n"
    for vid, ms, ts in rows:
        msg += f"🎬 `{vid}` → {format_views(ms)}\n"

    await interaction.response.send_message(msg)

def check_milestones(video_id, new_views, channel):
    c.execute(
        "SELECT milestone, ping FROM milestones WHERE video_id=?",
        (video_id,)
    )
    milestones = c.fetchall()

    for milestone, ping in milestones:
        if new_views >= milestone:
            c.execute(
                "SELECT 1 FROM reached_milestones WHERE video_id=? AND milestone=?",
                (video_id, milestone)
            )
            if c.fetchone():
                continue  # already announced

            c.execute(
                "INSERT INTO reached_milestones VALUES (?,?,?)",
                (video_id, milestone, int(time.time()))
            )
            conn.commit()

            msg = (
                f"🎉 **MILESTONE REACHED!**\n"
                f"🎬 `{video_id}`\n"
                f"🏁 {format_views(milestone)} views\n"
            )
            if ping:
                msg += f"\n{ping}"

            asyncio.create_task(channel.send(msg))

@tree.command(name="setinterval")
async def setinterval(
    interaction: discord.Interaction,
    url: str,
    hours: int
):
    if hours < 1:
        await interaction.response.send_message("❌ Interval must be ≥ 1 hour")
        return

    video_id = extract_video_id(url)
    if not video_id:
        await interaction.response.send_message("❌ Invalid YouTube URL")
        return

    next_run = int(time.time()) + (hours * 3600)

    c.execute("""
        INSERT OR REPLACE INTO intervals
        (video_id, interval_hours, next_run)
        VALUES (?,?,?)
    """, (video_id, hours, next_run))

    conn.commit()

    await interaction.response.send_message(
        f"✅ **Custom Interval Set**\n"
        f"🎬 `{video_id}`\n"
        f"⏱ Every `{hours}` hours"
    )

@tree.command(name="disableinterval")
async def disableinterval(interaction: discord.Interaction, url: str):
    video_id = extract_video_id(url)
    if not video_id:
        await interaction.response.send_message("❌ Invalid URL")
        return

    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    conn.commit()

    await interaction.response.send_message("🛑 Custom interval disabled")

async def custom_interval_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        now = int(time.time())

        c.execute("""
            SELECT video_id, interval_hours, next_run
            FROM intervals
            WHERE next_run <= ?
        """, (now,))
        rows = c.fetchall()

        for video_id, hours, next_run in rows:
            c.execute(
                "SELECT channel_id FROM videos WHERE video_id=?",
                (video_id,)
            )
            row = c.fetchone()
            if not row:
                continue

            channel = bot.get_channel(row[0])
            if not channel:
                continue

            await track_video(video_id, channel)

            next_time = now + (hours * 3600)
            c.execute(
                "UPDATE intervals SET next_run=? WHERE video_id=?",
                (next_time, video_id)
            )
            conn.commit()

        await asyncio.sleep(60)

@tree.command(name="views")
async def views(interaction: discord.Interaction, url: str):
    await interaction.response.defer()

    video_id = extract_video_id(url)
    if not video_id:
        await interaction.followup.send("❌ Invalid YouTube URL")
        return

    views = get_views(video_id)
    if views is None:
        await interaction.followup.send("❌ Failed to fetch views")
        return

    await interaction.followup.send(
        f"👁️ **Current Views**\n"
        f"🎬 `{video_id}`\n"
        f"📊 {format_views(views)}"
    )

@tree.command(name="forcecheck")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()

    channel_id = interaction.channel_id

    c.execute("""
        SELECT video_id, last_views
        FROM videos
        WHERE channel_id=?
    """, (channel_id,))
    videos = c.fetchall()

    if not videos:
        await interaction.followup.send("📭 No videos tracked in this channel")
        return

    for video_id, last_views in videos:
        new_views = get_views(video_id)
        if new_views is None:
            continue

        diff = new_views - last_views
        diff_text = f"+{format_views(diff)}" if diff >= 0 else "0"

        await interaction.followup.send(
            f"📈 **Force Check**\n"
            f"🎬 `{video_id}`\n"
            f"👁️ {format_views(new_views)} ({diff_text})"
        )

        c.execute(
            "UPDATE videos SET last_views=?, last_checked=? WHERE video_id=?",
            (new_views, int(time.time()), video_id)
        )
        conn.commit()

@tree.command(name="viewsall")
async def viewsall(interaction: discord.Interaction):
    await interaction.response.defer()

    guild_id = interaction.guild_id

    c.execute("""
        SELECT video_id, last_views
        FROM videos
        WHERE guild_id=?
    """, (guild_id,))
    videos = c.fetchall()

    if not videos:
        await interaction.followup.send("📭 No videos tracked in this server")
        return

    for video_id, last_views in videos:
        views = get_views(video_id)
        if views is None:
            continue

        await interaction.followup.send(
            f"🎬 `{video_id}`\n"
            f"👁️ {format_views(views)}"
        )

@tree.command(name="listvideos")
async def listvideos(interaction: discord.Interaction):
    channel_id = interaction.channel_id

    c.execute("""
        SELECT video_id
        FROM videos
        WHERE channel_id=?
    """, (channel_id,))
    rows = c.fetchall()

    if not rows:
        await interaction.response.send_message("📭 No videos in this channel")
        return

    msg = "📺 **Tracked Videos (This Channel)**\n\n"
    for (vid,) in rows:
        msg += f"• `{vid}`\n"

    await interaction.response.send_message(msg)
    
@tree.command(name="serverlist")
async def serverlist(interaction: discord.Interaction):
    guild_id = interaction.guild_id

    c.execute("""
        SELECT video_id
        FROM videos
        WHERE guild_id=?
    """, (guild_id,))
    rows = c.fetchall()

    if not rows:
        await interaction.response.send_message("📭 No videos in this server")
        return

    msg = "🌐 **Server Tracked Videos**\n\n"
    for (vid,) in rows:
        msg += f"• `{vid}`\n"

    await interaction.response.send_message(msg)
    
@tree.command(name="upcomingmilestones")
async def upcomingmilestones(interaction: discord.Interaction):
    await interaction.response.defer()

    guild_id = interaction.guild_id

    c.execute("""
        SELECT v.video_id, m.milestone
        FROM videos v
        JOIN milestones m ON v.video_id = m.video_id
        WHERE v.guild_id = ?
    """, (guild_id,))
    rows = c.fetchall()

    if not rows:
        await interaction.followup.send("📭 No milestones found")
        return

    sent = False

    for video_id, milestone in rows:
        views = get_views(video_id)
        if views is None:
            continue

        remaining = milestone - views
        if 0 < remaining <= 100_000:
            await interaction.followup.send(
                f"⏳ **Upcoming Milestone**\n"
                f"🎬 `{video_id}`\n"
                f"🎯 {format_views(milestone)}\n"
                f"📉 {format_views(remaining)} to go"
            )
            sent = True

    if not sent:
        await interaction.followup.send("✅ No videos close to milestones")
        
async def upcoming_milestone_alert(guild_id):
    c.execute("""
        SELECT v.video_id, v.channel_id, m.milestone, m.ping
        FROM videos v
        JOIN milestones m ON v.video_id = m.video_id
        WHERE v.guild_id = ?
    """, (guild_id,))
    rows = c.fetchall()

    ping_text = set()

    for video_id, channel_id, milestone, ping in rows:
        views = get_views(video_id)
        if views is None:
            continue

        remaining = milestone - views
        if 0 < remaining <= 100_000:
            channel = bot.get_channel(channel_id)
            if not channel:
                continue

            await channel.send(
                f"⏳ **Upcoming Milestone Summary**\n"
                f"🎬 `{video_id}`\n"
                f"🎯 {format_views(milestone)}\n"
                f"📉 {format_views(remaining)} remaining"
            )

            if ping:
                ping_text.add(ping)

    if ping_text:
        for channel_id in set(r[1] for r in rows):
            channel = bot.get_channel(channel_id)
            if channel:
                await channel.send(" ".join(ping_text))

KST = timezone(timedelta(hours=9))

TRACK_HOURS_KST = [0, 12, 17]  # 12AM, 12PM, 5PM

def seconds_until_next_kst_run():
    now = datetime.now(KST)

    today_targets = [
        now.replace(hour=h, minute=0, second=0, microsecond=0)
        for h in TRACK_HOURS_KST
    ]

    future = [t for t in today_targets if t > now]

    if future:
        next_run = min(future)
    else:
        next_run = (now + timedelta(days=1)).replace(
            hour=TRACK_HOURS_KST[0],
            minute=0,
            second=0,
            microsecond=0
        )

    return (next_run - now).total_seconds()

async def kst_tracking_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        wait_seconds = seconds_until_next_kst_run()
        await asyncio.sleep(wait_seconds)

        print("⏰ KST TRACKING START")

        c.execute("SELECT DISTINCT guild_id FROM videos")
        guilds = c.fetchall()

        for (guild_id,) in guilds:
            c.execute("""
                SELECT video_id, channel_id, last_views
                FROM videos
                WHERE guild_id=?
            """, (guild_id,))
            videos = c.fetchall()

            for video_id, channel_id, last_views in videos:
                channel = bot.get_channel(channel_id)
                if not channel:
                    continue

                new_views = get_views(video_id)
                if new_views is None:
                    continue

                diff = new_views - last_views

                await channel.send(
                    f"📊 **Tracking Update**\n"
                    f"🎬 `{video_id}`\n"
                    f"👁️ {format_views(new_views)} "
                    f"(+{format_views(diff)})"
                )

                c.execute("""
                    UPDATE videos
                    SET last_views=?, last_checked=?
                    WHERE video_id=?
                """, (new_views, int(time.time()), video_id))
                conn.commit()

                check_milestones(video_id, new_views, channel)

            # UPCOMING MILESTONE SUMMARY (ONCE PER SERVER)
            await upcoming_milestone_alert(guild_id)

        # prevent double-fire within same minute
        await asyncio.sleep(60)

from flask import Flask
from threading import Thread

app = Flask("")

@app.route("/")
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# START keep-alive
keep_alive()
