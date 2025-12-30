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
CUSTOM_INTERVAL = None  # seconds (optional)

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
bot = commands.Bot(command_prefix="!", intents=intents)

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
async def run_tracker():
    for vid, info in DATA["videos"].items():
        views = fetch_views(vid)
        if views is None:
            continue

        last_views = info.get("last_views", views)
        last_milestone = info.get(
            "last_milestone",
            last_views // MILESTONE_STEP
        )

        info["last_views"] = views

        current_milestone = views // MILESTONE_STEP
        if (
            info.get("milestone_ping")
            and current_milestone > last_milestone
        ):
            channel = bot.get_channel(info["channel_id"])
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
            await run_tracker()
            DATA["last_run"]["AM"] = today_str
            save_data()

        # PM slot: noon
        if now.hour == 12 and DATA["last_run"].get("PM") != today_str:
            await run_tracker()
            DATA["last_run"]["PM"] = today_str
            save_data()

        # sleep 5 minutes
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
        "last_views": 0,
        "last_milestone": 0,
        "milestone_ping": None
    }
    save_data()
    await interaction.response.send_message(
        f"✅ Tracking **{title}**",
        ephemeral=True
    )

@bot.tree.command(name="removevideo", description="Remove a tracked video")
async def removevideo(interaction: discord.Interaction, video_id: str):
    if video_id in DATA["videos"]:
        del DATA["videos"][video_id]
        save_data()
        await interaction.response.send_message(
            "🗑️ Video removed",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ Video not found",
            ephemeral=True
        )

@bot.tree.command(name="listvideos", description="List tracked videos")
async def listvideos(interaction: discord.Interaction):
    if not DATA["videos"]:
        await interaction.response.send_message("No videos tracked.")
        return

    msg = "\n".join(
        f"- {v['title']} (`{vid}`)"
        for vid, v in DATA["videos"].items()
    )
    await interaction.response.send_message(msg)

@bot.tree.command(name="setmilestone", description="Set milestone alert")
async def setmilestone(interaction: discord.Interaction, video_id: str, ping: str):
    if video_id not in DATA["videos"]:
        await interaction.response.send_message(
            "❌ Video not found",
            ephemeral=True
        )
        return

    DATA["videos"][video_id]["milestone_ping"] = ping
    save_data()
    await interaction.response.send_message(
        "🎯 Milestone alert set",
        ephemeral=True
    )

@bot.tree.command(name="removemilestone", description="Remove milestone alert")
async def removemilestone(interaction: discord.Interaction, video_id: str):
    if video_id not in DATA["videos"]:
        await interaction.response.send_message(
            "❌ Video not found",
            ephemeral=True
        )
        return

    DATA["videos"][video_id]["milestone_ping"] = None
    save_data()
    await interaction.response.send_message(
        "🚫 Milestone removed",
        ephemeral=True
    )

@bot.tree.command(name="setinterval", description="Set custom tracking interval (hours)")
async def setinterval(interaction: discord.Interaction, hours: int):
    global CUSTOM_INTERVAL
    if hours < 1:
        await interaction.response.send_message(
            "❌ Interval must be at least 1 hour",
            ephemeral=True
        )
        return
    CUSTOM_INTERVAL = hours * 60 * 60
    await interaction.response.send_message(
        f"⏱️ Custom interval set to **every {hours} hour(s)**",
        ephemeral=True
    )

@bot.tree.command(name="disableinterval", description="Disable custom tracking interval")
async def disableinterval(interaction: discord.Interaction):
    global CUSTOM_INTERVAL
    CUSTOM_INTERVAL = None
    await interaction.response.send_message(
        "🛑 Custom interval disabled (12-hour clock tracking still active)",
        ephemeral=True
    )
    
@bot.tree.command(name="viewsall", description="Show current views for all tracked videos")
async def viewsall(interaction: discord.Interaction):
    messages = []
    for vid, info in DATA["videos"].items():
        # Only include videos posted in this channel's server
        if info["channel_id"] != interaction.channel_id:
            continue
        current_views = fetch_views(vid)
        if current_views is None:
            messages.append(f"⚠ {info['title']} → could not fetch views")
        else:
            info["last_views"] = current_views
            messages.append(f"**{info['title']}** → {current_views:,} views")
    save_data()

    if messages:
        # Discord messages have a max length, so split if needed
        CHUNK_SIZE = 2000
        msg = "\n".join(messages)
        for i in range(0, len(msg), CHUNK_SIZE):
            await interaction.channel.send(msg[i:i+CHUNK_SIZE])
        await interaction.response.send_message("📊 All views updated!", ephemeral=True)
    else:
        await interaction.response.send_message("No videos tracked for this server.", ephemeral=True)
        
# =======================
# NEW COMMAND: /views
# =======================
@bot.tree.command(name="views", description="Get current views for a video")
async def views(interaction: discord.Interaction, video_id: str):
    if video_id not in DATA["videos"]:
        await interaction.response.send_message(
            "❌ Video not found",
            ephemeral=True
        )
        return

    current_views = fetch_views(video_id)
    if current_views is None:
        await interaction.response.send_message(
            "⚠ Could not fetch views right now",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"👀 **{DATA['videos'][video_id]['title']}** has **{current_views:,} views**",
        ephemeral=False
    )

# =======================
# UPDATED /forcecheck (reports views)
# =======================
@bot.tree.command(name="forcecheck", description="Run tracker now and show views")
async def forcecheck(interaction: discord.Interaction):
    messages = []
    for vid, info in DATA["videos"].items():
        current_views = fetch_views(vid)
        if current_views is None:
            continue
        info["last_views"] = current_views
        messages.append(f"**{info['title']}** → {current_views:,} views")
    save_data()

    if messages:
        await interaction.response.send_message(
            "🔁 Force check done:\n" + "\n".join(messages)
        )
    else:
        await interaction.response.send_message("🔁 Force check done, no data fetched")

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
