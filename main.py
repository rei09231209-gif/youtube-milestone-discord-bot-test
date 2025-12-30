import os
import json
import asyncio
import discord
from discord.ext import commands
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import requests

# =======================
# ENV VARIABLES
# =======================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not DISCORD_TOKEN or not YOUTUBE_API_KEY:
    raise RuntimeError("Missing DISCORD_TOKEN or YOUTUBE_API_KEY")

# =======================
# CONSTANTS
# =======================
KST = ZoneInfo("Asia/Seoul")
DATA_FILE = "videos.json"
CHECK_INTERVAL = 300  # 5 minutes
MILESTONE_STEP = 1_000_000
CUSTOM_INTERVAL = None  # seconds

# =======================
# DATA STORAGE
# =======================
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r") as f:
        DATA = json.load(f)
else:
    DATA = {"videos": {}, "last_run": {"AM": None, "PM": None}}

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(DATA, f, indent=2)

# =======================
# DISCORD BOT
# =======================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix=None, intents=intents)

# =======================
# YOUTUBE API
# =======================
def fetch_views(video_id: str):
    url = (
        "https://www.googleapis.com/youtube/v3/videos"
        "?part=statistics"
        f"&id={video_id}"
        f"&key={YOUTUBE_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data["items"]:
            return None
        return int(data["items"][0]["statistics"]["viewCount"])
    except Exception:
        return None

# =======================
# TRACKER CORE
# =======================
async def run_tracker(post_channel=None):
    for vid, info in DATA["videos"].items():
        views = fetch_views(vid)
        if views is None:
            continue

        last_views = info.get("last_views", 0)
        net_increase = views - last_views
        # store last_views for /botcheck net increase
        info["prev_views"] = last_views
        info["last_views"] = views

        # Post tracking update
        channel = bot.get_channel(info["channel_id"])
        if post_channel:
            channel = post_channel
        if channel:
            await channel.send(
                f"📈 **{info['title']}**: {views:,} views "
                f"(+{net_increase:,} since last check)"
            )

        # Milestone check
        last_milestone = info.get("last_milestone", 0)
        current_milestone = views // MILESTONE_STEP
        if info.get("milestone_ping") and current_milestone > last_milestone:
            if channel:
                await channel.send(
                    f"{info['milestone_ping']}\n"
                    f"🎉 **{info['title']}** reached "
                    f"**{current_milestone}M views!**"
                )
            info["last_milestone"] = current_milestone

    save_data()

# =======================
# CLOCK-BASED SCHEDULER
# =======================
async def scheduler_clock():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(KST)
        today_str = str(now.date())

        # AM slot: midnight
        if now.hour == 0 and DATA["last_run"].get("AM") != today_str:
            channel_ids = {v["channel_id"] for v in DATA["videos"].values()}
            for cid in channel_ids:
                ch = bot.get_channel(cid)
                if ch:
                    await run_tracker(post_channel=ch)
            DATA["last_run"]["AM"] = today_str
            save_data()

        # PM slot: noon
        if now.hour == 12 and DATA["last_run"].get("PM") != today_str:
            channel_ids = {v["channel_id"] for v in DATA["videos"].values()}
            for cid in channel_ids:
                ch = bot.get_channel(cid)
                if ch:
                    await run_tracker(post_channel=ch)
            DATA["last_run"]["PM"] = today_str
            save_data()

        await asyncio.sleep(CHECK_INTERVAL)

# =======================
# CUSTOM INTERVAL SCHEDULER
# =======================
async def custom_scheduler():
    await bot.wait_until_ready()
    while not bot.is_closed():
        if CUSTOM_INTERVAL:
            await asyncio.sleep(CUSTOM_INTERVAL)
            await run_tracker()
        else:
            await asyncio.sleep(60)

# =======================
# SLASH COMMANDS
# =======================
@bot.tree.command(name="addvideo", description="Add a video to track")
async def addvideo(interaction: discord.Interaction, video_id: str, title: str):
    DATA["videos"][video_id] = {
        "title": title,
        "channel_id": interaction.channel_id,
        "guild_id": interaction.guild_id,
        "last_views": 0,
        "prev_views": 0,
        "last_milestone": 0,
        "milestone_ping": None
    }
    save_data()
    await interaction.response.send_message(f"✅ Tracking **{title}**", ephemeral=True)

@bot.tree.command(name="removevideo", description="Remove a tracked video")
async def removevideo(interaction: discord.Interaction, video_id: str):
    if video_id in DATA["videos"]:
        del DATA["videos"][video_id]
        save_data()
        await interaction.response.send_message("🗑️ Video removed", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Video not found", ephemeral=True)

@bot.tree.command(name="listvideos", description="List tracked videos")
async def listvideos(interaction: discord.Interaction):
    if not DATA["videos"]:
        await interaction.response.send_message("No videos tracked.")
        return
    msg = "\n".join(f"- {v['title']} (`{vid}`)" for vid, v in DATA["videos"].items())
    await interaction.response.send_message(msg)

@bot.tree.command(name="setmilestone", description="Set milestone alert")
async def setmilestone(interaction: discord.Interaction, video_id: str, ping: str):
    if video_id not in DATA["videos"]:
        await interaction.response.send_message("❌ Video not found", ephemeral=True)
        return
    DATA["videos"][video_id]["milestone_ping"] = ping
    save_data()
    await interaction.response.send_message("🎯 Milestone alert set", ephemeral=True)

@bot.tree.command(name="removemilestone", description="Remove milestone alert")
async def removemilestone(interaction: discord.Interaction, video_id: str):
    if video_id not in DATA["videos"]:
        await interaction.response.send_message("❌ Video not found", ephemeral=True)
        return
    DATA["videos"][video_id]["milestone_ping"] = None
    save_data()
    await interaction.response.send_message("🚫 Milestone removed", ephemeral=True)

@bot.tree.command(name="setinterval", description="Set custom tracking interval (hours)")
async def setinterval(interaction: discord.Interaction, hours: int):
    global CUSTOM_INTERVAL
    if hours < 1:
        await interaction.response.send_message("❌ Interval must be at least 1 hour", ephemeral=True)
        return
    CUSTOM_INTERVAL = hours * 3600
    await interaction.response.send_message(f"⏱️ Custom interval set to every {hours} hour(s)", ephemeral=True)

@bot.tree.command(name="disableinterval", description="Disable custom tracking interval")
async def disableinterval(interaction: discord.Interaction):
    global CUSTOM_INTERVAL
    CUSTOM_INTERVAL = None
    await interaction.response.send_message("🛑 Custom interval disabled (12-hour clock tracking still active)", ephemeral=True)

@bot.tree.command(name="views", description="Get current views for a video")
async def views(interaction: discord.Interaction, video_id: str):
    if video_id not in DATA["videos"]:
        await interaction.response.send_message("❌ Video not found", ephemeral=True)
        return
    current_views = fetch_views(video_id)
    if current_views is None:
        await interaction.response.send_message("⚠ Could not fetch views right now", ephemeral=True)
        return
    last_views = DATA["videos"][video_id].get("last_views", 0)
    net_increase = current_views - last_views
    await interaction.response.send_message(f"👀 **{DATA['videos'][video_id]['title']}**: {current_views:,} views (+{net_increase:,} since last check)", ephemeral=False)

@bot.tree.command(name="forcecheck", description="Run tracker now for this channel")
async def forcecheck(interaction: discord.Interaction):
    await run_tracker(post_channel=bot.get_channel(interaction.channel_id))
    await interaction.response.send_message("🔁 Force check done for this channel!", ephemeral=True)

@bot.tree.command(name="viewsall", description="Show current views for all tracked videos in this server")
async def viewsall(interaction: discord.Interaction):
    messages = []
    for vid, info in DATA["videos"].items():
        if info.get("guild_id") != interaction.guild_id:
            continue
        views = fetch_views(vid)
        if views is None:
            messages.append(f"⚠ {info['title']} → could not fetch views")
        else:
            last_views = info.get("last_views", 0)
            net_increase = views - last_views
            info["prev_views"] = last_views
            info["last_views"] = views
            messages.append(f"**{info['title']}** → {views:,} (+{net_increase:,} since last check)")
    save_data()
    if messages:
        CHUNK_SIZE = 2000
        msg = "\n".join(messages)
        for i in range(0, len(msg), CHUNK_SIZE):
            await interaction.channel.send(msg[i:i+CHUNK_SIZE])
        await interaction.response.send_message("📊 Server-wide views updated!", ephemeral=True)
    else:
        await interaction.response.send_message("No videos tracked in this server.", ephemeral=True)

@bot.tree.command(name="botcheck", description="Check tracking status of videos")
async def botcheck(interaction: discord.Interaction):
    now = datetime.now(KST)
    today_str = str(now.date())
    msg_lines = []

    for vid, info in DATA["videos"].items():
        if info.get("guild_id") != interaction.guild_id:
            continue
        
        title = info["title"]
        am_done = "✅" if DATA["last_run"].get("AM") == today_str else "❌"
        pm_done = "✅" if DATA["last_run"].get("PM") == today_str else "❌"
        net_increase = info.get("last_views", 0) - info.get("prev_views", 0)
        msg_lines.append(
            f"**{title}**\n"
            f"  12 AM KST: {am_done}\n"
            f"  12 PM KST: {pm_done}\n"
            f"  Net increase since last check: {net_increase:,}\n"
        )
        info["prev_views"] = info.get("last_views", 0)
    
    save_data()
    if msg_lines:
        await interaction.response.send_message("\n".join(msg_lines))
    else:
        await interaction.response.send_message("No videos tracked in this server.")

# =======================
# KEEP ALIVE SERVER
# =======================
def keep_alive():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"alive")
        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

# =======================
# EVENTS
# =======================
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")
    bot.loop.create_task(scheduler_clock())
    bot.loop.create_task(custom_scheduler())

# =======================
# START BOT
# =======================
threading.Thread(target=keep_alive, daemon=True).start()
bot.run(DISCORD_TOKEN)
