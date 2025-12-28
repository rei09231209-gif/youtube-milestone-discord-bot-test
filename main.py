import discord
from discord.ext import commands, tasks
from discord import app_commands
import httpx
import json
import os
import asyncio
from datetime import datetime, time, timedelta
import pytz

# ================= ENV =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

KST = pytz.timezone("Asia/Seoul")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ================= STORAGE =================
DATA_FILE = "tracked_videos.json"

if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump([], f)

with open(DATA_FILE, "r") as f:
    tracked = json.load(f)

def save():
    with open(DATA_FILE, "w") as f:
        json.dump(tracked, f, indent=2)

# ================= YOUTUBE =================
async def get_views(video_id: str):
    url = (
        "https://www.googleapis.com/youtube/v3/videos"
        f"?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
    )
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        data = r.json()
        if data.get("items"):
            return int(data["items"][0]["statistics"]["viewCount"])
    return None

# ================= KST SCHEDULER =================
async def wait_until_next_kst_checkpoint():
    now = datetime.now(KST)

    today_midnight = datetime.combine(now.date(), time(0, 0), tzinfo=KST)
    today_noon = datetime.combine(now.date(), time(12, 0), tzinfo=KST)

    if now < today_midnight:
        target = today_midnight
    elif now < today_noon:
        target = today_noon
    else:
        target = today_midnight + timedelta(days=1)

    sleep_seconds = (target - now).total_seconds()
    await asyncio.sleep(sleep_seconds)

@tasks.loop(hours=12)
async def tracker():
    for item in tracked:
        views = await get_views(item["video_id"])
        if views is None:
            continue

        channel = bot.get_channel(item["channel_id"])
        if not channel:
            continue

        diff = views - item["last_views"]
        item["last_views"] = views
        save()

        await channel.send(
            f"📊 **{item['title']}**\n"
            f"Views: **{views:,}**\n"
            f"Change since last check: **+{diff:,}**"
        )

# ================= SLASH COMMANDS =================
@tree.command(name="addvideo", description="Track a YouTube video in this channel")
async def addvideo(interaction: discord.Interaction, title: str, video_id: str):
    views = await get_views(video_id)
    if views is None:
        await interaction.response.send_message("❌ Invalid video ID", ephemeral=True)
        return

    tracked.append({
        "title": title,
        "video_id": video_id,
        "channel_id": interaction.channel_id,
        "last_views": views
    })
    save()

    await interaction.response.send_message(
        f"✅ Tracking **{title}**\nCurrent views: {views:,}"
    )

@tree.command(name="removevideo", description="Stop tracking a video in this channel")
async def removevideo(interaction: discord.Interaction, video_id: str):
    for item in tracked:
        if item["video_id"] == video_id and item["channel_id"] == interaction.channel_id:
            tracked.remove(item)
            save()
            await interaction.response.send_message("🗑️ Video removed")
            return

    await interaction.response.send_message("⚠️ Video not found", ephemeral=True)

@tree.command(name="listvideos", description="List tracked videos in this channel")
async def listvideos(interaction: discord.Interaction):
    vids = [
        f"• {v['title']} ({v['video_id']})"
        for v in tracked
        if v["channel_id"] == interaction.channel_id
    ]

    if not vids:
        await interaction.response.send_message("No tracked videos", ephemeral=True)
        return

    await interaction.response.send_message("\n".join(vids), ephemeral=True)

# ================= READY =================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await tree.sync()

    # Align to next KST checkpoint before starting loop
    await wait_until_next_kst_checkpoint()

    if not tracker.is_running():
        tracker.start()

bot.run(DISCORD_TOKEN)
