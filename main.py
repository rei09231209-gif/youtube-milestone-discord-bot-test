import discord
from discord.ext import commands
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

# ================= IN-MEMORY STORAGE =================
# Structure:
# tracked_videos[channel_id] = [
#   {"title", "video_id", "last_views", "last_milestone", "milestone_ping"}
# ]
tracked_videos = {}

# ================= YOUTUBE API =================
async def get_views(video_id):
    url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        data = r.json()
        if data.get("items"):
            return int(data["items"][0]["statistics"]["viewCount"])
    return None

# ================= TRACKER =================
async def run_tracker_for_channel(channel_id):
    if channel_id not in tracked_videos:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    for video in tracked_videos[channel_id]:
        views = await get_views(video["video_id"])
        if views is None:
            continue

        diff = views - video["last_views"]
        current_milestone = views // 1_000_000

        # Normal view update (no ping)
        await channel.send(f"📊 **{video['title']}**\nViews: **{views:,}**\nChange: **+{diff:,}**")

        # Milestone alert (1M) only once
        if video.get("milestone_ping") and current_milestone > video["last_milestone"]:
            await channel.send(f"🎉 **{video['title']} reached {current_milestone}M views!**\n{video['milestone_ping']}")
            video["last_milestone"] = current_milestone

        video["last_views"] = views

# ================= KST SCHEDULING =================
async def start_kst_tracker():
    while True:
        now = datetime.now(KST)
        midnight = datetime.combine(now.date(), time(0,0), tzinfo=KST)
        noon = datetime.combine(now.date(), time(12,0), tzinfo=KST)

        if now < midnight:
            target = midnight
        elif now < noon:
            target = noon
        else:
            target = midnight + timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        print(f"Waiting {wait_seconds/3600:.2f} hours until next tracker run at {target.time()}")
        await asyncio.sleep(wait_seconds)

        for channel_id in tracked_videos:
            try:
                await run_tracker_for_channel(channel_id)
            except Exception as e:
                print(f"Error running tracker for channel {channel_id}: {e}")

# ================= SLASH COMMANDS =================
@tree.command(name="addvideo", description="Add a video to tracking")
async def addvideo(interaction: discord.Interaction, title: str, video_id: str):
    await interaction.response.send_message("⏳ Adding video...", ephemeral=True)
    views = await get_views(video_id)
    if views is None:
        await interaction.followup.send("❌ Invalid video ID", ephemeral=True)
        return

    if interaction.channel.id not in tracked_videos:
        tracked_videos[interaction.channel.id] = []

    tracked_videos[interaction.channel.id].append({
        "title": title,
        "video_id": video_id,
        "last_views": views,
        "last_milestone": views // 1_000_000,
        "milestone_ping": None
    })

    await interaction.followup.send(f"✅ Tracking **{title}**\nCurrent views: {views:,}", ephemeral=True)

@tree.command(name="removevideo", description="Remove a tracked video")
async def removevideo(interaction: discord.Interaction, video_id: str):
    if interaction.channel.id not in tracked_videos:
        await interaction.response.send_message("No videos tracked.", ephemeral=True)
        return

    new_list = [v for v in tracked_videos[interaction.channel.id] if v["video_id"] != video_id]
    if len(new_list) == len(tracked_videos[interaction.channel.id]):
        await interaction.response.send_message("Video not found.", ephemeral=True)
        return

    tracked_videos[interaction.channel.id] = new_list
    await interaction.response.send_message("🗑️ Video removed.", ephemeral=True)

@tree.command(name="listvideos", description="List tracked videos")
async def listvideos(interaction: discord.Interaction):
    if interaction.channel.id not in tracked_videos or not tracked_videos[interaction.channel.id]:
        await interaction.response.send_message("No videos tracked.", ephemeral=True)
        return

    msg = "\n".join(f"• {v['title']} ({v['video_id']}) → last milestone: {v['last_milestone']}M"
                    for v in tracked_videos[interaction.channel.id])
    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="setmilestone", description="Set milestone alert for a video")
async def setmilestone(interaction: discord.Interaction, video_id: str, ping_message: str):
    if interaction.channel.id not in tracked_videos:
        await interaction.response.send_message("No videos tracked.", ephemeral=True)
        return
    for video in tracked_videos[interaction.channel.id]:
        if video["video_id"] == video_id:
            video["milestone_ping"] = ping_message
            await interaction.response.send_message(f"Milestone alert set for **{video['title']}**", ephemeral=True)
            return
    await interaction.response.send_message("Video not found.", ephemeral=True)

@tree.command(name="removemilestone", description="Remove milestone alert from a video")
async def removemilestone(interaction: discord.Interaction, video_id: str):
    if interaction.channel.id not in tracked_videos:
        await interaction.response.send_message("No videos tracked.", ephemeral=True)
        return
    for video in tracked_videos[interaction.channel.id]:
        if video["video_id"] == video_id:
            video["milestone_ping"] = None
            await interaction.response.send_message(f"Milestone alert removed from **{video['title']}**", ephemeral=True)
            return
    await interaction.response.send_message("Video not found.", ephemeral=True)

@tree.command(name="listmilestones", description="List videos with milestone alerts")
async def listmilestones(interaction: discord.Interaction):
    if interaction.channel.id not in tracked_videos:
        await interaction.response.send_message("No videos tracked.", ephemeral=True)
        return
    msg = "\n".join(f"• {v['title']} ({v['video_id']}) → Ping: {v['milestone_ping']}" 
                    for v in tracked_videos[interaction.channel.id] if v.get("milestone_ping"))
    if not msg:
        msg = "No milestone alerts set."
    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="views", description="Get current views of a video")
async def views(interaction: discord.Interaction, video_id: str):
    await interaction.response.send_message("⏳ Fetching views...", ephemeral=True)
    views_count = await get_views(video_id)
    if views_count is None:
        await interaction.followup.send("❌ Invalid video ID.", ephemeral=True)
        return
    await interaction.followup.send(f"👁️ Views: **{views_count:,}**", ephemeral=True)

@tree.command(name="forcecheck", description="Manually trigger tracker for this channel")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.send_message("⏳ Running tracker...", ephemeral=True)
    await run_tracker_for_channel(interaction.channel.id)
    await interaction.followup.send("✅ Tracker run completed.", ephemeral=True)

# ================= READY =================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await tree.sync()
    asyncio.create_task(start_kst_tracker())

bot.run(DISCORD_TOKEN)
