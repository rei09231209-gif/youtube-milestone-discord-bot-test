# ==========================
# MAIN.PY - FULL VERSION
# YouTube Tracker Bot
# ==========================

# PART 1 — Imports, DB, Helpers
import discord
from discord.ext import tasks
from discord import app_commands
import sqlite3
import aiohttp
import os
from datetime import datetime, timedelta
import threading
from flask import Flask

# ---------- ENV VARIABLES ----------
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8080))

# ---------- KST Helper ----------
def now_kst():
    return datetime.utcnow() + timedelta(hours=9)

# ---------- SQLite DB ----------
db = sqlite3.connect("yt_tracker.db")
c = db.cursor()

c.execute("""CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    title TEXT,
    guild_id TEXT,
    channel_id TEXT,
    alert_channel TEXT
)""")

c.execute("""CREATE TABLE IF NOT EXISTS milestones (
    video_id TEXT PRIMARY KEY,
    last_alerted_milestone INTEGER DEFAULT 0,
    ping_message TEXT DEFAULT ""
)""")

c.execute("""CREATE TABLE IF NOT EXISTS intervals (
    video_id TEXT PRIMARY KEY,
    interval_hours REAL,
    next_run TEXT
)""")

c.execute("""CREATE TABLE IF NOT EXISTS upcoming_alerts (
    guild_id TEXT PRIMARY KEY,
    channel_id TEXT,
    ping_message TEXT
)""")

db.commit()

# ---------- YouTube API Fetch ----------
async def fetch_views(video_id):
    url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            items = data.get("items")
            if not items:
                return None
            return int(items[0]["statistics"]["viewCount"])

# ==========================
# PART 2 — Flask Keep-Alive
# ==========================
app = Flask("")

@app.route("/")
def home():
    return "Bot is alive! 🌟"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

threading.Thread(target=run_flask).start()

# ==========================
# PART 3 — Discord Bot Setup
# ==========================
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Bot(intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ✅")
    kst_tracker_loop.start()
    custom_interval_loop.start()

# ==========================
# PART 4 — KST Tracker (12 AM, 12 PM, 5 PM)
# ==========================
TRACK_HOURS = [0, 12, 17]  # 12 AM, 12 PM, 5 PM KST

@tasks.loop(minutes=1)
async def kst_tracker_loop():
    now = now_kst()
    if now.hour in TRACK_HOURS and now.minute == 0:
        c.execute("SELECT video_id, title, guild_id, channel_id, alert_channel FROM videos")
        videos = c.fetchall()
        upcoming_alerts_sent = {}
        for vid, title, guild_id, channel_id, alert_channel in videos:
            views = await fetch_views(vid)
            if views is None:
                continue

            # Automatic Milestone check
            c.execute("SELECT last_alerted_milestone, ping_message FROM milestones WHERE video_id=?", (vid,))
            result = c.fetchone()
            if result:
                last_alerted, ping_message = result
                milestone = (views // 1_000_000) * 1_000_000
                if milestone > last_alerted:
                    msg = f"{ping_message} 🏆 **{title}** reached **{milestone} views!** 🎉"
                    await bot.get_channel(alert_channel).send(msg)
                    c.execute("UPDATE milestones SET last_alerted_milestone=? WHERE video_id=?", (milestone, vid))
                    db.commit()

            # Upcoming Milestones Summary
            c.execute("SELECT guild_id, channel_id, ping_message FROM upcoming_alerts WHERE guild_id=?", (guild_id,))
            alert = c.fetchone()
            if alert:
                guild_id_alert, channel_id_alert, ping_msg = alert
                next_milestone = ((views // 1_000_000) + 1) * 1_000_000
                if next_milestone - views <= 100_000:
                    await bot.get_channel(channel_id_alert).send(
                        f"⏳ **{title}** is {next_milestone - views} views away from {next_milestone}!"
                    )
                    upcoming_alerts_sent[guild_id_alert] = (channel_id_alert, ping_msg)

        # Send one final ping at end per guild
        for guild_id, (channel_id_alert, ping_msg) in upcoming_alerts_sent.items():
            if ping_msg:
                await bot.get_channel(channel_id_alert).send(f"{ping_msg} 🔔")

# ==========================
# PART 5 — Custom Interval Tracker
# ==========================
@tasks.loop(minutes=5)
async def custom_interval_loop():
    now = now_kst()
    c.execute("SELECT v.video_id, interval_hours, next_run, v.channel_id, v.title FROM videos v JOIN intervals i ON v.video_id=i.video_id")
    for video_id, interval_hours, next_run_str, channel_id, title in c.fetchall():
        next_run = datetime.fromisoformat(next_run_str) if next_run_str else now
        if now >= next_run:
            views = await fetch_views(video_id)
            if views is None:
                continue

            # Update next run
            next_run = now + timedelta(hours=interval_hours)
            c.execute("UPDATE intervals SET next_run=? WHERE video_id=?", (next_run.isoformat(), video_id))
            db.commit()

            # Automatic Milestone check
            c.execute("SELECT last_alerted_milestone, ping_message FROM milestones WHERE video_id=?", (video_id,))
            result = c.fetchone()
            if result:
                last_alerted, ping_message = result
                milestone = (views // 1_000_000) * 1_000_000
                if milestone > last_alerted:
                    await bot.get_channel(channel_id).send(f"{ping_message} 🏆 **{title}** reached **{milestone} views!** 🎉")
                    c.execute("UPDATE milestones SET last_alerted_milestone=? WHERE video_id=?", (milestone, video_id))
                    db.commit()

# ==========================
# PART 6 — Slash Commands
# ==========================

# ---------- Video Commands ----------
@bot.tree.command(name="addvideo", description="Add a video to track")
@app_commands.describe(video_id="YouTube Video ID", title="Video title", alert_channel="Channel for milestone alerts")
async def addvideo(interaction: discord.Interaction, video_id: str, title: str, alert_channel: discord.TextChannel):
    c.execute("SELECT video_id FROM videos WHERE video_id=? AND guild_id=?", (video_id, interaction.guild.id))
    if c.fetchone():
        await interaction.response.send_message(f"{title} is already tracked!", ephemeral=True)
        return
    c.execute("INSERT INTO videos (video_id, title, guild_id, channel_id, alert_channel) VALUES (?, ?, ?, ?, ?)",
              (video_id, title, str(interaction.guild.id), str(interaction.channel.id), str(alert_channel.id)))
    db.commit()
    await interaction.response.send_message(f"✅ Added **{title}** for tracking!", ephemeral=True)


@bot.tree.command(name="removevideo", description="Remove a tracked video")
@app_commands.describe(video_id="YouTube Video ID")
async def removevideo(interaction: discord.Interaction, video_id: str):
    c.execute("DELETE FROM videos WHERE video_id=? AND guild_id=?", (video_id, interaction.guild.id))
    c.execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    db.commit()
    await interaction.response.send_message(f"🗑 Removed video {video_id} from tracking!", ephemeral=True)


@bot.tree.command(name="listvideos", description="List videos tracked in this channel")
async def listvideos(interaction: discord.Interaction):
    c.execute("SELECT title, video_id FROM videos WHERE channel_id=?", (str(interaction.channel.id),))
    videos = c.fetchall()
    if not videos:
        await interaction.response.send_message("No videos tracked in this channel.", ephemeral=True)
        return
    msg = "\n".join([f"📌 **{title}** ({vid})" for title, vid in videos])
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="serverlist", description="List all videos tracked in this server")
async def serverlist(interaction: discord.Interaction):
    c.execute("SELECT title, video_id, alert_channel FROM videos WHERE guild_id=?", (str(interaction.guild.id),))
    videos = c.fetchall()
    if not videos:
        await interaction.response.send_message("No videos tracked in this server.", ephemeral=True)
        return
    msg = "\n".join([f"📌 **{title}** ({vid}) - Alert: <#{alert_chan}>" for title, vid, alert_chan in videos])
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="viewsall", description="Show current views for all videos in the server")
async def viewsall(interaction: discord.Interaction):
    await interaction.response.defer()
    c.execute("SELECT title, video_id FROM videos WHERE guild_id=?", (str(interaction.guild.id),))
    videos = c.fetchall()
    if not videos:
        await interaction.followup.send("No videos tracked in this server.")
        return
    for title, vid in videos:
        views = await fetch_views(vid)
        if views is not None:
            await interaction.followup.send(f"👀 **{title}**: {views} views")
        else:
            await interaction.followup.send(f"⚠️ **{title}**: Could not fetch views")


# ---------- Milestone Commands ----------
@bot.tree.command(name="setmilestone", description="Set automatic milestone alerts for a video")
@app_commands.describe(video_id="YouTube Video ID", ping_message="Ping message for milestone alerts")
async def setmilestone(interaction: discord.Interaction, video_id: str, ping_message: str = ""):
    c.execute("SELECT video_id FROM videos WHERE video_id=? AND guild_id=?", (video_id, interaction.guild.id))
    if not c.fetchone():
        await interaction.response.send_message("Video not tracked yet.", ephemeral=True)
        return
    c.execute("INSERT OR REPLACE INTO milestones (video_id, last_alerted_milestone, ping_message) VALUES (?, ?, ?)",
              (video_id, 0, ping_message))
    db.commit()
    await interaction.response.send_message(f"✅ Milestone alerts set for {video_id}", ephemeral=True)


@bot.tree.command(name="removemilestone", description="Remove milestone alerts for a video")
@app_commands.describe(video_id="YouTube Video ID")
async def removemilestone(interaction: discord.Interaction, video_id: str):
    c.execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    db.commit()
    await interaction.response.send_message(f"🗑 Removed milestone alerts for {video_id}", ephemeral=True)


@bot.tree.command(name="reachedmilestones", description="Show milestones reached in last 24 hours")
async def reachedmilestones(interaction: discord.Interaction):
    await interaction.response.defer()
    cutoff = now_kst() - timedelta(days=1)
    c.execute("SELECT video_id, last_alerted_milestone FROM milestones")
    milestones = c.fetchall()
    if not milestones:
        await interaction.followup.send("No milestones reached in the last 24 hours.")
        return
    for vid, ms in milestones:
        await interaction.followup.send(f"🏆 {vid}: {ms} views")


# ---------- Forcecheck per channel ----------
@bot.tree.command(name="forcecheck", description="Force check views for all videos in this channel")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()
    c.execute("SELECT title, video_id FROM videos WHERE channel_id=?", (str(interaction.channel.id),))
    videos = c.fetchall()
    if not videos:
        await interaction.followup.send("No videos tracked in this channel.")
        return
    for title, vid in videos:
        views = await fetch_views(vid)
        if views is not None:
            await interaction.followup.send(f"👀 {title}: {views} views")
        else:
            await interaction.followup.send(f"⚠️ {title}: Could not fetch views")


# ---------- Botcheck ----------
@bot.tree.command(name="botcheck", description="Check last KST tracker status")
async def botcheck(interaction: discord.Interaction):
    now = now_kst()
    last_run = f"🕒 Last tracker run: {now.strftime('%Y-%m-%d %H:%M:%S')} KST"
    await interaction.response.send_message(last_run, ephemeral=True)


# ---------- Upcoming Milestones Commands ----------
@bot.tree.command(name="upcomingmilestones", description="Show upcoming milestones for all server videos")
async def upcomingmilestones(interaction: discord.Interaction):
    await interaction.response.defer()
    c.execute("SELECT video_id, title FROM videos WHERE guild_id=?", (str(interaction.guild.id),))
    videos = c.fetchall()
    if not videos:
        await interaction.followup.send("No videos tracked in this server.")
        return
    for vid, title in videos:
        views = await fetch_views(vid)
        if views is None:
            continue
        next_milestone = ((views // 1_000_000) + 1) * 1_000_000
        if next_milestone - views <= 100_000:
            await interaction.followup.send(f"⏳ **{title}** is {next_milestone - views} views away from {next_milestone}")


@bot.tree.command(name="setupupcomingmilestonesalert", description="Set the channel and ping for automatic upcoming milestones summary")
@app_commands.describe(channel="Channel to post summary", ping_message="Custom ping at end")
async def setupupcomingmilestonesalert(interaction: discord.Interaction, channel: discord.TextChannel, ping_message: str = ""):
    c.execute("INSERT OR REPLACE INTO upcoming_alerts (guild_id, channel_id, ping_message) VALUES (?, ?, ?)",
              (str(interaction.guild.id), str(channel.id), ping_message))
    db.commit()
    await interaction.response.send_message(f"✅ Upcoming Milestones Summary set in {channel.mention}", ephemeral=True)


# ---------- Custom Interval Commands ----------
@bot.tree.command(name="setinterval", description="Set a custom tracking interval (hours) for a video")
@app_commands.describe(video_id="YouTube Video ID", interval_hours="Interval in hours")
async def setinterval(interaction: discord.Interaction, video_id: str, interval_hours: float):
    c.execute("SELECT video_id FROM videos WHERE video_id=? AND guild_id=?", (video_id, interaction.guild.id))
    if not c.fetchone():
        await interaction.response.send_message("Video not tracked yet.", ephemeral=True)
        return
    next_run = now_kst() + timedelta(hours=interval_hours)
    c.execute("INSERT OR REPLACE INTO intervals (video_id, interval_hours, next_run) VALUES (?, ?, ?)",
              (video_id, interval_hours, next_run.isoformat()))
    db.commit()
    await interaction.response.send_message(f"⏱ Custom interval of {interval_hours}h set for {video_id}", ephemeral=True)


@bot.tree.command(name="disableinterval", description="Disable custom interval tracking for a video")
@app_commands.describe(video_id="YouTube Video ID")
async def disableinterval(interaction: discord.Interaction, video_id: str):
    c.execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
    db.commit()
    await interaction.response.send_message(f"🛑 Custom interval disabled for {video_id}", ephemeral=True)


# ==========================
# PART 7 — Start Bot
# ==========================
if __name__ == "__main__":
    try:
        print("Starting bot...")
        bot.run(BOT_TOKEN)
    except Exception as e:
        print(f"Error starting bot: {e}")
