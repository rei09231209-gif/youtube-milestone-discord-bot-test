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

KST = pytz.timezone("Asia/Seoul")

# ================= DISCORD BOT =================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

PIN_HEADER = "YT_TRACK_DATA"

# ================= YOUTUBE API =================
async def get_views(video_id):
    url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        data = r.json()
        if data.get("items"):
            return int(data["items"][0]["statistics"]["viewCount"])
    return None

# ================= PIN STORAGE =================
async def get_pin(channel):
    pins = await channel.pins()
    for p in pins:
        if p.content.startswith(PIN_HEADER):
            return p
    msg = await channel.send(PIN_HEADER)
    await msg.pin()
    return msg

def parse_pin(content):
    videos = []
    lines = content.split("\n")[1:]
    for line in lines:
        try:
            title, vid, last, milestone, ping = line.split("|")
            videos.append({
                "title": title,
                "video_id": vid,
                "last_views": int(last),
                "last_milestone": int(milestone),
                "ping": ping
            })
        except:
            pass
    return videos

def build_pin(videos):
    lines = [PIN_HEADER]
    for v in videos:
        lines.append(f"{v['title']}|{v['video_id']}|{v['last_views']}|{v['last_milestone']}|{v['ping']}")
    return "\n".join(lines)

# ================= TRACKER LOGIC =================
async def run_tracker_for_channel(channel):
    pin = await get_pin(channel)
    videos = parse_pin(pin.content)
    if not videos:
        return

    updated = False
    for v in videos:
        views = await get_views(v["video_id"])
        if views is None:
            continue

        diff = views - v["last_views"]
        current_milestone = views // 1_000_000

        # View update
        await channel.send(f"📊 **{v['title']}**\nViews: **{views:,}**\nChange: **+{diff:,}**")

        # Milestone alert
        if current_milestone > v["last_milestone"]:
            await channel.send(f"🎉 **{v['title']} reached {current_milestone}M views!**\n{v['ping']}")
            v["last_milestone"] = current_milestone

        v["last_views"] = views
        updated = True

    if updated:
        await pin.edit(content=build_pin(videos))

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
    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                await run_tracker_for_channel(channel)
            except:
                continue

# ================= SLASH COMMANDS =================
@tree.command(name="addvideo", description="Add a video to tracking (with custom ping)")
async def addvideo(interaction: discord.Interaction, title: str, video_id: str, ping_message: str = " "):
    views = await get_views(video_id)
    if views is None:
        await interaction.response.send_message("Invalid video ID", ephemeral=True)
        return

    pin = await get_pin(interaction.channel)
    videos = parse_pin(pin.content)

    videos.append({
        "title": title,
        "video_id": video_id,
        "last_views": views,
        "last_milestone": views // 1_000_000,
        "ping": ping_message
    })

    await pin.edit(content=build_pin(videos))
    await interaction.response.send_message(f"✅ Tracking **{title}**\nCurrent views: {views:,}\nPing message: `{ping_message}`")

@tree.command(name="removevideo", description="Remove a tracked video")
async def removevideo(interaction: discord.Interaction, video_id: str):
    pin = await get_pin(interaction.channel)
    videos = parse_pin(pin.content)
    new_videos = [v for v in videos if v["video_id"] != video_id]

    if len(new_videos) == len(videos):
        await interaction.response.send_message("Video not found", ephemeral=True)
        return

    await pin.edit(content=build_pin(new_videos))
    await interaction.response.send_message("🗑️ Video removed")

@tree.command(name="listvideos", description="List tracked videos")
async def listvideos(interaction: discord.Interaction):
    pin = await get_pin(interaction.channel)
    videos = parse_pin(pin.content)
    if not videos:
        await interaction.response.send_message("No videos tracked", ephemeral=True)
        return
    msg = "\n".join(f"• {v['title']} ({v['video_id']}) → last milestone: {v['last_milestone']}M" for v in videos)
    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="views", description="Get current views of a video")
async def views(interaction: discord.Interaction, video_id: str):
    v = await get_views(video_id)
    if v is None:
        await interaction.response.send_message("Invalid video ID", ephemeral=True)
        return
    await interaction.response.send_message(f"👁️ Views: **{v:,}**")

@tree.command(name="forcecheck", description="Manually trigger the tracker for this channel")
async def forcecheck(interaction: discord.Interaction):
    await run_tracker_for_channel(interaction.channel)
    await interaction.response.send_message("✅ Tracker run completed for this channel")

# ================= READY =================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await tree.sync()
    await wait_until_kst_checkpoint()
    if not tracker.is_running():
        tracker.start()

bot.run(DISCORD_TOKEN)
