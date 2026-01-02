import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import sqlite3
from datetime import datetime, timedelta
import pytz
import requests
import json

# --------------------- DATABASE SETUP ---------------------
conn = sqlite3.connect('yt_tracker.db')
c = conn.cursor()

# Table for videos
c.execute('''
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    guild_id TEXT,
    channel_id TEXT,
    title TEXT,
    views INTEGER,
    last_checked TEXT
)
''')

# Table for milestones
c.execute('''
CREATE TABLE IF NOT EXISTS milestones (
    video_id TEXT,
    milestone INTEGER,
    ping TEXT,
    PRIMARY KEY (video_id, milestone)
)
''')

# Table for custom intervals
c.execute('''
CREATE TABLE IF NOT EXISTS intervals (
    video_id TEXT PRIMARY KEY,
    hours REAL
)
''')

conn.commit()

#--------------------- KEEP ALIVE SETUP ----------------------
from threading import Thread
from flask import Flask

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

# --------------------- HELPER FUNCTIONS ---------------------
def now_kst():
    tz = pytz.timezone('Asia/Seoul')
    return datetime.now(tz)

def format_views(num):
    # Format large numbers nicely, e.g., 1.2M
    if num >= 1_000_000:
        return f"{num/1_000_000:.1f}M"
    elif num >= 1_000:
        return f"{num/1_000:.1f}K"
    else:
        return str(num)

def fetch_views(video_id, api_key="YOUR_YOUTUBE_API_KEY"):
    # Replace with actual YouTube API request
    url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={video_id}&key={api_key}"
    try:
        response = requests.get(url).json()
        return int(response['items'][0]['statistics']['viewCount'])
    except Exception as e:
        print(f"Error fetching views for {video_id}: {e}")
        return None

TRACK_TIMES_KST = [(0, 0), (12, 0), (17, 0)]  # 12AM, 12PM, 5PM

# --------------------- BOT SETUP ---------------------
intents = discord.Intents.default()
intents.message_content = True  # Required for slash commands

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# --------------------- VIDEO COMMANDS ---------------------

@tree.command(name="addvideo", description="Add a video to track")
@app_commands.describe(video_id="YouTube Video ID", channel_id="Discord channel to post updates")
async def addvideo(interaction: discord.Interaction, video_id: str, channel_id: str):
    guild_id = str(interaction.guild.id)

    # Fetch title and current views
    views = fetch_views(video_id)
    if views is None:
        await interaction.response.send_message("❌ Failed to fetch video info.", ephemeral=True)
        return

    # Dummy title placeholder (you can enhance to fetch actual title)
    title = f"Video {video_id}"

    # Insert or replace into SQLite
    c.execute('''
        INSERT OR REPLACE INTO videos (video_id, guild_id, channel_id, title, views, last_checked)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (video_id, guild_id, channel_id, title, views, now_kst().isoformat()))
    conn.commit()

    await interaction.response.send_message(f"✅ Added **{title}** to tracking!")

@tree.command(name="removevideo", description="Remove a tracked video")
@app_commands.describe(video_id="YouTube Video ID")
async def removevideo(interaction: discord.Interaction, video_id: str):
    guild_id = str(interaction.guild.id)

    c.execute('DELETE FROM videos WHERE video_id=? AND guild_id=?', (video_id, guild_id))
    c.execute('DELETE FROM milestones WHERE video_id=?', (video_id,))
    c.execute('DELETE FROM intervals WHERE video_id=?', (video_id,))
    conn.commit()

    await interaction.response.send_message(f"🗑️ Removed video {video_id} from tracking.")

@tree.command(name="listvideos", description="List all videos tracked in this server")
async def listvideos(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    c.execute('SELECT video_id, title, views, channel_id FROM videos WHERE guild_id=?', (guild_id,))
    rows = c.fetchall()

    if not rows:
        await interaction.response.send_message("No videos tracked in this server.", ephemeral=True)
        return

    msg = ""
    for vid_id, title, views, channel_id in rows:
        msg += f"🎬 {title} ({vid_id}) — {format_views(views)} views | Channel: <#{channel_id}>\n"

    # Split into chunks if too long
    for chunk_start in range(0, len(msg), 2000):
        await interaction.response.send_message(msg[chunk_start:chunk_start+2000])

# --------------------- MILESTONE COMMANDS ---------------------

@tree.command(name="setmilestone", description="Set a milestone alert for a video")
@app_commands.describe(video_id="YouTube Video ID", milestone="Milestone in views", ping="Optional role/user ping")
async def setmilestone(interaction: discord.Interaction, video_id: str, milestone: int, ping: str = ""):
    guild_id = str(interaction.guild.id)

    # Check video exists
    c.execute('SELECT title FROM videos WHERE video_id=? AND guild_id=?', (video_id, guild_id))
    row = c.fetchone()
    if not row:
        await interaction.response.send_message("❌ Video not found in this server.", ephemeral=True)
        return

    title = row[0]

    # Insert milestone
    c.execute('INSERT OR REPLACE INTO milestones (video_id, milestone, ping) VALUES (?, ?, ?)',
              (video_id, milestone, ping))
    conn.commit()

    await interaction.response.send_message(f"✅ Milestone {format_views(milestone)} set for **{title}**")

@tree.command(name="removemilestone", description="Remove a milestone from a video")
@app_commands.describe(video_id="YouTube Video ID", milestone="Milestone in views to remove")
async def removemilestone(interaction: discord.Interaction, video_id: str, milestone: int):
    guild_id = str(interaction.guild.id)

    c.execute('DELETE FROM milestones WHERE video_id=? AND milestone=?', (video_id, milestone))
    conn.commit()

    await interaction.response.send_message(f"🗑️ Milestone {format_views(milestone)} removed for video {video_id}")

@tree.command(name="listmilestones", description="List milestones for a video")
@app_commands.describe(video_id="YouTube Video ID")
async def listmilestones(interaction: discord.Interaction, video_id: str):
    guild_id = str(interaction.guild.id)

    c.execute('SELECT milestone, ping FROM milestones WHERE video_id=?', (video_id,))
    rows = c.fetchall()

    if not rows:
        await interaction.response.send_message("No milestones set for this video.", ephemeral=True)
        return

    msg = f"📊 **Milestones for video {video_id}:**\n"
    for m, ping in rows:
        msg += f"- {format_views(m)}"
        if ping:
            msg += f" (ping: {ping})"
        msg += "\n"

    await interaction.response.send_message(msg)

async def tracking_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        now = now_kst()
        next_run = None

        for hour, minute in TRACK_TIMES_KST:
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > now:
                next_run = candidate
                break

        if not next_run:
            hour, minute = TRACK_TIMES_KST[0]
            next_run = (now + timedelta(days=1)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )

        sleep_seconds = (next_run - now).total_seconds()
        await asyncio.sleep(sleep_seconds)

        await run_tracking_cycle()

async def run_tracking_cycle():
    c.execute('SELECT video_id, guild_id, channel_id, title, views FROM videos')
    videos = c.fetchall()

    for video_id, guild_id, channel_id, title, old_views in videos:
        new_views = fetch_views(video_id)
        if new_views is None:
            continue

        diff = new_views - old_views
        now_time = now_kst().isoformat()

        # Update database
        c.execute(
            'UPDATE videos SET views=?, last_checked=? WHERE video_id=?',
            (new_views, now_time, video_id)
        )

        channel = bot.get_channel(int(channel_id))
        if channel and diff > 0:
            await channel.send(
                f"📊 **Tracking Update**\n"
                f"🎬 **{title}**\n"
                f"➕ **+{format_views(diff)} views**\n"
                f"👁️ Total: **{format_views(new_views)}**"
            )

        # -------- Milestone Reached Check --------
        c.execute('SELECT milestone, ping FROM milestones WHERE video_id=?', (video_id,))
        milestones = c.fetchall()

        for milestone, ping in milestones:
            if old_views < milestone <= new_views:
                await channel.send(
                    f"🎉 **MILESTONE REACHED!**\n"
                    f"🎬 **{title}**\n"
                    f"🏁 **{format_views(milestone)} views**\n"
                    f"{ping}"
                )

    conn.commit()

    # ---------- PART 5: FORCECHECK & MILESTONE CHECK ----------

async def run_tracking_cycle(tag: str, guild_id=None, channel_id=None):
    """
    Runs a tracking cycle for videos.
    - tag: string label like "00:00" KST
    - guild_id: optional, track only this server
    - channel_id: optional, track only this channel
    """
    query = "SELECT video_id, guild_id, channel_id, title, last_views FROM videos"
    params = ()

    if guild_id and channel_id:
        query += " WHERE guild_id=? AND channel_id=?"
        params = (guild_id, channel_id)
    elif guild_id:
        query += " WHERE guild_id=?"
        params = (guild_id,)

    c.execute(query, params)
    videos = c.fetchall()

    for video in videos:
        video_id, g_id, ch_id, title, last_views = video
        current_views = fetch_views(video_id)
        if current_views is None:
            continue

        net_increase = current_views - (last_views or current_views)
        if net_increase <= 0:
            continue

        channel = bot.get_channel(int(ch_id))
        if not channel:
            continue

        # Send net increase tracking message
        await channel.send(
            f"📊 **Tracking Update ({tag} KST)**\n"
            f"🎬 **{title}**\n"
            f"📈 +{format_views(net_increase)} → {format_views(current_views)}"
        )

        # Update SQLite
        c.execute(
            "UPDATE videos SET last_views=?, last_checked=? WHERE video_id=?",
            (current_views, int(time.time()), video_id)
        )
        conn.commit()

        # Check milestones
        await check_milestones(video_id, current_views, channel)


async def check_milestones(video_id, current_views, channel):
    """
    Checks milestones for a video and sends milestone messages.
    - Only triggers once per milestone.
    """
    c.execute(
        "SELECT milestone, ping FROM milestones WHERE video_id=?",
        (video_id,)
    )
    milestones = c.fetchall()

    for milestone, ping in milestones:
        # Already reached?
        c.execute(
            "SELECT 1 FROM reached_milestones WHERE video_id=? AND milestone=?",
            (video_id, milestone)
        )
        if c.fetchone():
            continue

        if current_views >= milestone:
            msg = (
                f"🎉 **MILESTONE REACHED!**\n"
                f"🎬 `{video_id}`\n"
                f"🏁 {format_views(milestone)}"
            )
            if ping:
                msg += f"\n{ping}"

            await channel.send(msg)

            # Save reached milestone
            c.execute(
                "INSERT INTO reached_milestones VALUES (?,?,?)",
                (video_id, milestone, int(time.time()))
            )
            conn.commit()


# ---------------- FORCECHECK COMMAND ----------------
@bot.tree.command(name="forcecheck")
async def forcecheck(interaction: discord.Interaction):
    """
    Force updates all videos in this channel only.
    """
    await interaction.response.defer()
    await run_tracking_cycle(tag="FORCECHECK", guild_id=interaction.guild_id, channel_id=interaction.channel_id)
    await interaction.followup.send("✅ Forcecheck complete for this channel!")
    
@bot.tree.command(name="addvideo")
async def addvideo(interaction: discord.Interaction, url: str):
    await interaction.response.defer(ephemeral=True)

    video_id = extract_video_id(url)
    if not video_id:
        await interaction.followup.send("❌ Invalid YouTube URL")
        return

    views = fetch_views(video_id)
    if views is None:
        await interaction.followup.send("❌ Could not fetch views")
        return

    c.execute(
        'INSERT OR IGNORE INTO videos VALUES (?,?,?,?,?,?)',
        (
            video_id,
            interaction.guild_id,
            interaction.channel_id,
            f"Video {video_id}",
            views,
            now_kst().isoformat()
        )
    )
    conn.commit()

    await interaction.followup.send(
        f"✅ Video added\n"
        f"👁️ Current views: **{format_views(views)}**"
    )

@bot.tree.command(name="removevideo")
async def removevideo(interaction: discord.Interaction, url: str):
    video_id = extract_video_id(url)

    c.execute(
        'DELETE FROM videos WHERE video_id=? AND channel_id=?',
        (video_id, interaction.channel_id)
    )
    conn.commit()

    await interaction.response.send_message("🗑️ Video removed")

@bot.tree.command(name="listvideos")
async def listvideos(interaction: discord.Interaction):
    c.execute(
        'SELECT title, views FROM videos WHERE channel_id=?',
        (interaction.channel_id,)
    )
    rows = c.fetchall()

    if not rows:
        await interaction.response.send_message("📭 No videos tracked in this channel")
        return

    msg = "📺 **Tracked Videos (This Channel)**\n\n"
    for title, views in rows:
        msg += f"🎬 **{title}** — 👁️ {format_views(views)}\n"

    await interaction.response.send_message(msg)

@bot.tree.command(name="viewsall")
async def viewsall(interaction: discord.Interaction):
    await interaction.response.defer()

    c.execute(
        'SELECT title, views FROM videos WHERE guild_id=?',
        (interaction.guild_id,)
    )
    rows = c.fetchall()

    if not rows:
        await interaction.followup.send("📭 No videos tracked in this server")
        return

    chunk = ""
    for title, views in rows:
        line = f"🎬 **{title}** — 👁️ {format_views(views)}\n"
        if len(chunk) + len(line) > 1800:
            await interaction.followup.send(chunk)
            chunk = ""
        chunk += line

    if chunk:
        await interaction.followup.send(chunk)

@bot.tree.command(name="forcecheck")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()

    c.execute(
        'SELECT video_id, title, views FROM videos WHERE channel_id=?',
        (interaction.channel_id,)
    )
    rows = c.fetchall()

    if not rows:
        await interaction.followup.send("📭 No videos tracked here")
        return

    for video_id, title, old_views in rows:
        new_views = fetch_views(video_id)
        if new_views is None:
            continue

        diff = new_views - old_views
        c.execute(
            'UPDATE videos SET views=?, last_checked=? WHERE video_id=?',
            (new_views, now_kst().isoformat(), video_id)
        )

        if diff > 0:
            await interaction.followup.send(
                f"📊 **Force Check**\n"
                f"🎬 **{title}**\n"
                f"➕ **+{format_views(diff)}**\n"
                f"👁️ Total: {format_views(new_views)}"
            )

    conn.commit()

@bot.tree.command(name="views")
async def views(interaction: discord.Interaction, url: str):
    video_id = extract_video_id(url)
    if not video_id:
        await interaction.response.send_message("❌ Invalid YouTube URL")
        return

    views = fetch_views(video_id)
    if views is None:
        await interaction.response.send_message("❌ Failed to fetch views")
        return

    await interaction.response.send_message(
        f"👁️ **Current Views**\n"
        f"🎬 `{video_id}`\n"
        f"👀 {format_views(views)}"
    )

@bot.tree.command(name="serverlist")
async def serverlist(interaction: discord.Interaction):
    await interaction.response.defer()

    c.execute(
        'SELECT title, channel_id FROM videos WHERE guild_id=?',
        (interaction.guild_id,)
    )
    rows = c.fetchall()

    if not rows:
        await interaction.followup.send("📭 No videos tracked in this server")
        return

    msg = "📂 **Server Video List**\n\n"
    for title, channel_id in rows:
        channel = bot.get_channel(int(channel_id))
        ch_name = channel.name if channel else "unknown-channel"
        msg += f"🎬 **{title}** → #{ch_name}\n"

    await interaction.followup.send(msg)

@bot.tree.command(name="setmilestone")
async def setmilestone(
    interaction: discord.Interaction,
    url: str,
    milestone: int,
    ping: str = ""
):
    video_id = extract_video_id(url)

    c.execute(
        'INSERT INTO milestones VALUES (?,?,?)',
        (video_id, milestone, ping)
    )
    conn.commit()

    await interaction.response.send_message(
        f"🏁 Milestone set at **{format_views(milestone)}**"
    )

@bot.tree.command(name="removemilestone")
async def removemilestone(
    interaction: discord.Interaction,
    url: str,
    milestone: int
):
    video_id = extract_video_id(url)

    c.execute(
        'DELETE FROM milestones WHERE video_id=? AND milestone=?',
        (video_id, milestone)
    )
    conn.commit()

    await interaction.response.send_message("🗑️ Milestone removed")

@bot.tree.command(name="listmilestones")
async def listmilestones(interaction: discord.Interaction, url: str):
    video_id = extract_video_id(url)

    c.execute(
        'SELECT milestone FROM milestones WHERE video_id=? ORDER BY milestone',
        (video_id,)
    )
    rows = c.fetchall()

    if not rows:
        await interaction.response.send_message("📭 No milestones set")
        return

    msg = "🏁 **Milestones**\n"
    for (m,) in rows:
        msg += f"• {format_views(m)}\n"

    await interaction.response.send_message(msg)

@bot.tree.command(name="setinterval")
async def setinterval(
    interaction: discord.Interaction,
    url: str,
    hours: int
):
    video_id = extract_video_id(url)

    if hours < 1:
        await interaction.response.send_message("❌ Interval must be at least 1 hour")
        return

    c.execute(
        'UPDATE videos SET interval_hours=? WHERE video_id=?',
        (hours, video_id)
    )
    conn.commit()

    await interaction.response.send_message(
        f"⏱️ Custom interval set: every **{hours} hours**"
    )

@bot.tree.command(name="disableinterval")
async def disableinterval(interaction: discord.Interaction, url: str):
    video_id = extract_video_id(url)

    c.execute(
        'UPDATE videos SET interval_hours=NULL WHERE video_id=?',
        (video_id,)
    )
    conn.commit()

    await interaction.response.send_message("⛔ Custom interval disabled")

async def clock_scheduler():
    await bot.wait_until_ready()

    last_run = {
        "00:00": None,
        "12:00": None,
        "17:00": None
    }

    while not bot.is_closed():
        now_utc = datetime.utcnow()
        kst = now_utc + timedelta(hours=9)
        current_time = kst.strftime("%H:%M")

        if current_time in last_run:
            if last_run[current_time] != kst.date():
                last_run[current_time] = kst.date()
                await run_tracking_cycle(tag=current_time)

        await asyncio.sleep(60)

async def run_tracking_cycle(tag: str):
    c.execute('SELECT * FROM videos')
    videos = c.fetchall()

    for video in videos:
        (
            video_id, guild_id, channel_id,
            title, last_views,
            last_checked, interval_hours
        ) = video

        current_views = fetch_views(video_id)
        if current_views is None:
            continue

        diff = current_views - (last_views or current_views)

        if diff <= 0:
            continue

        channel = bot.get_channel(int(channel_id))
        if not channel:
            continue

        await channel.send(
            f"📊 **Tracking Update ({tag} KST)**\n"
            f"🎬 **{title}**\n"
            f"📈 +{format_views(diff)} → {format_views(current_views)}"
        )

        # save update
        c.execute(
            'UPDATE videos SET last_views=?, last_checked=? WHERE video_id=?',
            (current_views, int(time.time()), video_id
            )
        conn.commit()

        await check_milestones(video_id, current_views, channel)

    # after all videos → upcoming milestone summary
    await upcoming_milestone_summary(tag)

async def check_milestones(video_id, views, channel):
    c.execute(
        'SELECT milestone, ping FROM milestones WHERE video_id=?',
        (video_id,)
    )
    milestones = c.fetchall()

    for milestone, ping in milestones:
        c.execute(
            'SELECT 1 FROM reached_milestones WHERE video_id=? AND milestone=?',
            (video_id, milestone)
        )
        if c.fetchone():
            continue

        if views >= milestone:
            msg = (
                f"🎉 **MILESTONE REACHED!**\n"
                f"🎬 `{video_id}`\n"
                f"🏁 {format_views(milestone)}"
            )
            if ping:
                msg += f"\n{ping}"

            await channel.send(msg)

            c.execute(
                'INSERT INTO reached_milestones VALUES (?,?,?)',
                (video_id, milestone, int(time.time()))
            )
            conn.commit()

async def upcoming_milestone_summary(tag):
    c.execute('SELECT DISTINCT guild_id FROM videos')
    guilds = c.fetchall()

    for (guild_id,) in guilds:
        c.execute(
            '''
            SELECT v.video_id, v.title, v.channel_id, m.milestone, m.ping
            FROM videos v
            JOIN milestones m ON v.video_id = m.video_id
            '''
        )
        rows = c.fetchall()

        ping_to_send = None

        for video_id, title, channel_id, milestone, ping in rows:
            views = fetch_views(video_id)
            if views is None:
                continue

            remaining = milestone - views
            if remaining <= 0 or remaining > 100_000:
                continue

            channel = bot.get_channel(int(channel_id))
            if not channel:
                continue

            await channel.send(
                f"⏳ **Upcoming Milestone Summary ({tag} KST)**\n"
                f"🎬 **{title}**\n"
                                f"🏁 {format_views(milestone)}\n"
                f"📉 Remaining: {format_views(remaining)}"
            )

            if ping:
                ping_to_send = ping

        if ping_to_send:
            channel = bot.get_channel(int(rows[0][2]))
            if channel:
                await channel.send(ping_to_send)

@bot.tree.command(name="reachedmilestones")
async def reachedmilestones(interaction: discord.Interaction):
    since = int(time.time()) - 86400

    c.execute(
        '''
        SELECT video_id, milestone, timestamp
        FROM reached_milestones
        WHERE timestamp >= ?
        ORDER BY timestamp DESC
        ''',
        (since,)
    )
    rows = c.fetchall()

    if not rows:
        await interaction.response.send_message(
            "📭 No milestones reached in the past 24 hours"
        )
        return

    msg = "🏆 **Milestones Reached (Last 24h)**\n\n"
    for video_id, milestone, ts in rows:
        msg += f"🎬 `{video_id}` → {format_views(milestone)}\n"

    await interaction.response.send_message(msg)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")
    bot.loop.create_task(clock_scheduler())

keep_alive()
bot.run(os.environ["DISCORD_TOKEN"])


    
