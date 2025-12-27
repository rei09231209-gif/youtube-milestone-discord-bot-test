import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import json
import os

# ---------- CONFIG ----------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
ARTIST_NAME = os.getenv("ARTIST_NAME")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL"))

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# ---------- DATA FILES ----------
with open("videos.json", "r") as f:
    videos = json.load(f)

# ---------- MILESTONES ----------
# Automatically generate milestones from 1M to 1000M
milestones = [i * 1_000_000 for i in range(1, 1001)]

def save_videos():
    with open("videos.json", "w") as f:
        json.dump(videos, f, indent=2)

# ---------- YOUTUBE HELPER ----------
async def fetch_views(video_id):
    url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if data.get("items"):
                return int(data["items"][0]["statistics"]["viewCount"])
    return None

async def send_milestone_alert(title, views, milestone):
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(
        title="🎉 YouTube Milestone Reached!",
        color=0xff0000
    )
    embed.add_field(name="Artist", value=ARTIST_NAME, inline=True)
    embed.add_field(name="Video", value=title, inline=False)
    embed.add_field(name="Milestone", value=f"{milestone//1_000_000}M views", inline=True)
    embed.add_field(name="Current Views", value=f"{views:,}", inline=True)
    await channel.send(embed=embed)

# ---------- MILESTONE CHECK LOOP ----------
@tasks.loop(minutes=CHECK_INTERVAL)
async def check_milestones():
    for video in videos:
        views = await fetch_views(video["videoId"])
        if views is None:
            continue
        # Find next milestone passed
        milestone = next(
            (m for m in milestones if views >= m > video.get("lastMilestone", 0)),
            None
        )
        if milestone:
            await send_milestone_alert(video["title"], views, milestone)
            video["lastMilestone"] = milestone
            save_videos()

# ---------- SLASH COMMANDS ----------
@tree.command(name="addvideo", description="Add a YouTube video to track")
async def addvideo(interaction: discord.Interaction, title: str, videoid: str):
    if any(v["videoId"] == videoid for v in videos):
        await interaction.response.send_message("⚠️ Video already tracked.", ephemeral=True)
        return
    videos.append({"title": title, "videoId": videoid, "lastMilestone": 0})
    save_videos()
    await interaction.response.send_message(f"✅ **{title}** added for milestone tracking.")

@tree.command(name="removevideo", description="Remove a video from tracking")
async def removevideo(interaction: discord.Interaction, videoid: str):
    for v in videos:
        if v["videoId"] == videoid:
            videos.remove(v)
            save_videos()
            await interaction.response.send_message(f"❌ Removed **{v['title']}**.")
            return
    await interaction.response.send_message("⚠️ Video not found.", ephemeral=True)

@tree.command(name="listvideos", description="List all tracked videos")
async def listvideos(interaction: discord.Interaction):
    if not videos:
        await interaction.response.send_message("No videos tracked.", ephemeral=True)
        return
    msg = "\n".join([f"• {v['title']} ({v['videoId']})" for v in videos])
    await interaction.response.send_message(msg, ephemeral=True)

# ---------- READY EVENT ----------
@bot.event
async def on_ready():
    print(f"Bot logged in as {bot.user}")
    check_milestones.start()
    await tree.sync()

bot.run(DISCORD_TOKEN)

