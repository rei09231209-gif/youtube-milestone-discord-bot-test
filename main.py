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
import time

# =========================
# ENV
# =========================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not DISCORD_TOKEN or not YOUTUBE_API_KEY:
    raise RuntimeError("Missing env vars")

# =========================
# CONSTANTS
# =========================
KST = ZoneInfo("Asia/Seoul")
DATA_FILE = "videos.json"
MILESTONE_STEP = 1_000_000

# =========================
# DATA
# =========================
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r") as f:
        DATA = json.load(f)
else:
    DATA = {
        "videos": {},
        "last_run": {"AM": None, "PM": None}
    }

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(DATA, f, indent=2)

# =========================
# BOT
# =========================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix=None, intents=intents)

# =========================
# KEEP ALIVE
# =========================
class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Alive")

def run_web():
    HTTPServer(("0.0.0.0", 8080), KeepAlive).serve_forever()

threading.Thread(target=run_web, daemon=True).start()

# =========================
# YOUTUBE API
# =========================
def fetch_views(video_id):
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

# =========================
# HELPERS
# =========================
def videos_for_channel(channel_id):
    return {
        vid: info
        for vid, info in DATA["videos"].items()
        if info["channel_id"] == channel_id
    }

# =========================
# TRACK SINGLE VIDEO
# =========================
async def track_video(vid, info, channel, prefix="📊"):
    views = fetch_views(vid)
    if views is None:
        return

    last_views = info.get("last_views", 0)
    net = views - last_views

    info["prev_views"] = last_views
    info["last_views"] = views

    await channel.send(
        f"{prefix} **{info['title']}**\n"
        f"{views:,} views (+{net:,})"
    )

    last_m = info.get("last_milestone", 0)
    cur_m = views // MILESTONE_STEP

    if info.get("milestone_ping") and cur_m > last_m:
        await channel.send(
            f"{info['milestone_ping']}\n"
            f"🎉 **{info['title']}** reached **{cur_m}M views!**"
        )
        info["last_milestone"] = cur_m

# =========================
# 12 AM / 12 PM KST TRACKER
# =========================
async def clock_tracker():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(KST)
        today = str(now.date())

        if now.hour == 0 and DATA["last_run"]["AM"] != today:
            for vid, info in DATA["videos"].items():
                ch = bot.get_channel(info["channel_id"])
                if ch:
                    await track_video(vid, info, ch, "🕛 12AM KST")
            DATA["last_run"]["AM"] = today
            save_data()

        if now.hour == 12 and DATA["last_run"]["PM"] != today:
            for vid, info in DATA["videos"].items():
                ch = bot.get_channel(info["channel_id"])
                if ch:
                    await track_video(vid, info, ch, "🕛 12PM KST")
            DATA["last_run"]["PM"] = today
            save_data()

        await asyncio.sleep(60)

# =========================
# CUSTOM INTERVAL LOOP
# =========================
async def custom_interval_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = time.time()

        for vid, info in DATA["videos"].items():
            interval = info.get("custom_interval")
            if not interval:
                continue

            last = info.get("last_custom_check", 0)
            if now - last < interval:
                continue

            ch = bot.get_channel(info["channel_id"])
            if ch:
                await track_video(vid, info, ch, "⏱️ Interval")
                info["last_custom_check"] = now

        save_data()
        await asyncio.sleep(60)

# =========================
# SLASH COMMANDS
# =========================
@bot.tree.command(name="addvideo")
async def addvideo(interaction: discord.Interaction, video_id: str, title: str):
    DATA["videos"][video_id] = {
        "title": title,
        "channel_id": interaction.channel_id,
        "last_views": 0,
        "prev_views": 0,
        "last_milestone": 0,
        "milestone_ping": None,
        "custom_interval": None,
        "last_custom_check": 0
    }
    save_data()
    await interaction.response.send_message("✅ Video added")

@bot.tree.command(name="removevideo")
async def removevideo(interaction: discord.Interaction, video_id: str):
    DATA["videos"].pop(video_id, None)
    save_data()
    await interaction.response.send_message("🗑️ Video removed")

@bot.tree.command(name="listvideos")
async def listvideos(interaction: discord.Interaction):
    vids = videos_for_channel(interaction.channel_id)
    if not vids:
        await interaction.response.send_message("❌ No videos tracked here", ephemeral=True)
        return

    msg = "\n".join(f"• **{v['title']}** (`{vid}`)" for vid, v in vids.items())
    await interaction.response.send_message(msg)

@bot.tree.command(name="views")
async def views(interaction: discord.Interaction, video_id: str):
    info = DATA["videos"].get(video_id)
    if not info:
        await interaction.response.send_message("❌ Video not found", ephemeral=True)
        return

    views = fetch_views(video_id)
    await interaction.response.send_message(
        f"📊 **{info['title']}** — {views:,} views"
    )

@bot.tree.command(name="forcecheck")
async def forcecheck(interaction: discord.Interaction):
    vids = videos_for_channel(interaction.channel_id)
    if not vids:
        await interaction.response.send_message("❌ No videos here", ephemeral=True)
        return

    await interaction.response.defer()
    for vid, info in vids.items():
        await track_video(vid, info, interaction.channel)
    save_data()

@bot.tree.command(name="viewsall")
async def viewsall(interaction: discord.Interaction):
    if not DATA["videos"]:
        await interaction.response.send_message("❌ No videos tracked", ephemeral=True)
        return

    msg = ""
    for vid, info in DATA["videos"].items():
        views = fetch_views(vid)
        msg += f"• **{info['title']}** — {views:,}\n"

    await interaction.response.send_message(msg)

@bot.tree.command(name="setinterval")
async def setinterval(interaction: discord.Interaction, video_id: str, hours: int):
    if video_id in DATA["videos"]:
        DATA["videos"][video_id]["custom_interval"] = hours * 3600
        save_data()
        await interaction.response.send_message("⏱️ Interval set")

@bot.tree.command(name="disableinterval")
async def disableinterval(interaction: discord.Interaction, video_id: str):
    if video_id in DATA["videos"]:
        DATA["videos"][video_id]["custom_interval"] = None
        save_data()
        await interaction.response.send_message("⛔ Interval disabled")

@bot.tree.command(name="setmilestone")
async def setmilestone(interaction: discord.Interaction, video_id: str, ping: str):
    if video_id in DATA["videos"]:
        DATA["videos"][video_id]["milestone_ping"] = ping
        save_data()
        await interaction.response.send_message("🎯 Milestone set")

@bot.tree.command(name="removemilestone")
async def removemilestone(interaction: discord.Interaction, video_id: str):
    if video_id in DATA["videos"]:
        DATA["videos"][video_id]["milestone_ping"] = None
        save_data()
        await interaction.response.send_message("❌ Milestone removed")

@bot.tree.command(name="listmilestones")
async def listmilestones(interaction: discord.Interaction):
    vids = DATA["videos"]
    msg = ""
    for vid, info in vids.items():
        if info.get("milestone_ping"):
            msg += f"• **{info['title']}** — {info['milestone_ping']}\n"

    await interaction.response.send_message(msg or "❌ No milestones set")

@bot.tree.command(name="botcheck")
async def botcheck(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🛠️ **Bot status**\n"
        f"12 AM KST: {'✅' if DATA['last_run']['AM'] else '❌'}\n"
        f"12 PM KST: {'✅' if DATA['last_run']['PM'] else '❌'}"
    )

# =========================
# STARTUP
# =========================
@bot.event
async def on_ready():
    await bot.tree.sync()
    bot.loop.create_task(clock_tracker())
    bot.loop.create_task(custom_interval_loop())
    print("Bot ready")

bot.run(DISCORD_TOKEN)
