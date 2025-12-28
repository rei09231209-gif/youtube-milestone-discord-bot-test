import discord
from discord.ext import commands, tasks
from discord import app_commands
import httpx
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, time, timedelta
import pytz
import asyncio
import json

# ================= KEEP-ALIVE WEB SERVER =================
def run_web():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is alive")

    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

threading.Thread(target=run_web, daemon=True).start()

# ================= ENV =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
DATA_FILE = "tracked_videos.json"

KST = pytz.timezone("Asia/Seoul")

# ================= DISCORD BOT =================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Load or initialize JSON storage
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({}, f)
        
# ================= YOUTUBE API =================
async def get_views(video_id):
    url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        data = r.json()
        if data.get("items"):
            return int(data["items"][0]["statistics"]["viewCount"])
    return None

# ================= JSON STORAGE =================
def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)
        
# ================= TRACKER =================
async def run_tracker_for_channel(channel_id):
    data = load_data()
    if str(channel_id) not in data:
        return

    channel_data = data[str(channel_id)]
    updated = False

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    for video in channel_data:
        views = await get_views(video["video_id"])
        if views is None:
            continue

        diff = views - video["last_views"]
        current_milestone = views // 1_000_000

        # Send view update
        await channel.send(f"📊 **{video['title']}**\nViews: **{views:,}**\nChange: **+{diff:,}**")

        # Milestone alert (1M views)
        if current_milestone > video["last_milestone"]:
            await channel.send(f"🎉 **{video['title']} reached {current_milestone}M views!**\n{video['ping']}")
            video["last_milestone"] = current_milestone

        video["last_views"] = views
        updated = True

    if updated:
        save_data(data)

# ================= KST SCHEDULING =================
async def wait_until_kst_checkpoint():
    now = datetime.now(KST)
    midnight = datetime.combine(now.date(), time(0, 0), tzinfo=KST)
    noon = datetime.combine(now.date(), time(12, 0), tzinfo=KST)

    if now < midnight:
        target = midnight
    elif now < noon:
        target = noon
    else:
        target = midnight + timedelta(days=1)

    await asyncio.sleep((target - now).total_seconds())

@tasks.loop(hours=12)
async def tracker():
    data = load_data()
    for channel_id_str in data:
        channel_id = int(channel_id_str)
        try:
            await run_tracker_for_channel(channel_id)
        except:
            continue

# ================= SLASH COMMANDS =================
@tree.command(name="addvideo", description="Add a video to tracking (with custom ping)")
async def addvideo(interaction: discord.Interaction, title: str, video_id: str, ping_message: str = " "):
    await interaction.response.send_message("⏳ Adding video...", ephemeral=True)

    async def process():
        views = await get_views(video_id)
        if views is None:
            await interaction.followup.send("❌ Invalid video ID", ephemeral=True)
            return

        data = load_data()
        channel_id_str = str(interaction.channel.id)
        if channel_id_str not in data:
            data[channel_id_str] = []

        data[channel_id_str].append({
            "title": title,
            "video_id": video_id,
            "last_views": views,
            "last_milestone": views // 1_000_000,
            "ping": ping_message
        })
        save_data(data)
        await interaction.followup.send(f"✅ Tracking **{title}**\nCurrent views: {views:,}\nPing message: `{ping_message}`", ephemeral=True)

    asyncio.create_task(process())

@tree.command(name="removevideo", description="Remove a tracked video")
async def removevideo(interaction: discord.Interaction, video_id: str):
    data = load_data()
    channel_id_str = str(interaction.channel.id)
    if channel_id_str not in data:
        await interaction.response.send_message("No videos tracked in this channel.", ephemeral=True)
        return

    new_list = [v for v in data[channel_id_str] if v["video_id"] != video_id]
    if len(new_list) == len(data[channel_id_str]):
        await interaction.response.send_message("Video not found.", ephemeral=True)
        return

    data[channel_id_str] = new_list
    save_data(data)
    await interaction.response.send_message("🗑️ Video removed.", ephemeral=True)

@tree.command(name="listvideos", description="List tracked videos")
async def listvideos(interaction: discord.Interaction):
    data = load_data()
    channel_id_str = str(interaction.channel.id)
    if channel_id_str not in data or not data[channel_id_str]:
        await interaction.response.send_message("No videos tracked in this channel.", ephemeral=True)
        return

    msg = "\n".join(f"• {v['title']} ({v['video_id']}) → last milestone: {v['last_milestone']}M" for v in data[channel_id_str])
    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="views", description="Get current views of a video")
async def views(interaction: discord.Interaction, video_id: str):
    await interaction.response.send_message("⏳ Fetching views...", ephemeral=True)
    views_count = await get_views(video_id)
    if views_count is None:
        await interaction.followup.send("❌ Invalid video ID.", ephemeral=True)
        return
    await interaction.followup.send(f"👁️ Views: **{views_count:,}**", ephemeral=True)

@tree.command(name="forcecheck", description="Manually trigger the tracker for this channel")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.send_message("⏳ Running tracker...", ephemeral=True)
    await run_tracker_for_channel(interaction.channel.id)
    await interaction.followup.send("✅ Tracker run completed for this channel.", ephemeral=True)

# ================= READY =================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await tree.sync()
    # Wait until next KST checkpoint
    asyncio.create_task(wait_until_kst_checkpoint())
    # Start tracker loop if not running
    if not tracker.is_running():
        tracker.start()

bot.run(DISCORD_TOKEN)
    
