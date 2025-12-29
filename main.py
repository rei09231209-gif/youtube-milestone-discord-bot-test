import discord
from discord.ext import commands
from discord import app_commands
import httpx
import os
import asyncio
from datetime import datetime, time, timedelta
import pytz
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# =========================================================
# KEEP-ALIVE WEB SERVER (Render / UptimeRobot safe)
# =========================================================
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

threading.Thread(target=keep_alive, daemon=True).start()

# =========================================================
# ENV
# =========================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
KST = pytz.timezone("Asia/Seoul")

# =========================================================
# DISCORD BOT
# =========================================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# =========================================================
# IN-MEMORY STORAGE
# channel_id : list of video dicts
# =========================================================
tracked_videos = {}

# =========================================================
# YOUTUBE API
# =========================================================
async def get_views(video_id: str):
    url = (
        "https://www.googleapis.com/youtube/v3/videos"
        f"?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
    )
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        data = r.json()
        if data.get("items"):
            return int(data["items"][0]["statistics"]["viewCount"])
    return None

# =========================================================
# TRACKER
# =========================================================
async def run_tracker_for_channel(channel_id: int):
    if channel_id not in tracked_videos:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    for video in tracked_videos[channel_id]:
        try:
            views = await get_views(video["video_id"])
            if views is None:
                continue

            diff = views - video["last_views"]
            current_milestone = views // 1_000_000

            # Normal update (NO ping)
            await channel.send(
                f"📊 **{video['title']}**\n"
                f"Views: **{views:,}**\n"
                f"Change: **+{diff:,}**"
            )

            # Milestone alert (ONCE per 1M)
            if (
                video["milestone_ping"]
                and current_milestone > video["last_milestone"]
            ):
                await channel.send(
                    f"🎉 **{video['title']} reached {current_milestone}M views!**\n"
                    f"{video['milestone_ping']}"
                )
                video["last_milestone"] = current_milestone

            video["last_views"] = views

        except Exception as e:
            print(f"Tracker error: {e}")

# =========================================================
# KST SCHEDULER (12 AM / 12 PM)
# =========================================================
async def kst_scheduler():
    while True:
        now = datetime.now(KST)

        midnight = datetime.combine(now.date(), time(0, 0), tzinfo=KST)
        noon = datetime.combine(now.date(), time(12, 0), tzinfo=KST)

        if now < midnight:
            target = midnight
        elif now < noon:
            target = noon
        else:
            target = midnight + timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        print(f"Next tracker run at {target} KST")
        await asyncio.sleep(wait_seconds)

        for channel_id in list(tracked_videos.keys()):
            await run_tracker_for_channel(channel_id)

# =========================================================
# SLASH COMMANDS
# =========================================================
@tree.command(name="addvideo", description="Add a video to tracking")
async def addvideo(interaction: discord.Interaction, title: str, video_id: str):
    await interaction.response.send_message("Adding video...", ephemeral=True)

    views = await get_views(video_id)
    if views is None:
        await interaction.followup.send("Invalid video ID.", ephemeral=True)
        return

    channel_id = interaction.channel.id
    tracked_videos.setdefault(channel_id, []).append({
        "title": title,
        "video_id": video_id,
        "last_views": views,
        "last_milestone": views // 1_000_000,
        "milestone_ping": None
    })

    await interaction.followup.send(
        f"✅ Tracking **{title}**\nCurrent views: {views:,}",
        ephemeral=True
    )

@tree.command(name="removevideo", description="Remove a tracked video")
async def removevideo(interaction: discord.Interaction, video_id: str):
    channel_id = interaction.channel.id
    if channel_id not in tracked_videos:
        await interaction.response.send_message("No videos tracked.", ephemeral=True)
        return

    tracked_videos[channel_id] = [
        v for v in tracked_videos[channel_id] if v["video_id"] != video_id
    ]

    await interaction.response.send_message("Video removed.", ephemeral=True)

@tree.command(name="listvideos", description="List tracked videos")
async def listvideos(interaction: discord.Interaction):
    channel_id = interaction.channel.id
    if channel_id not in tracked_videos or not tracked_videos[channel_id]:
        await interaction.response.send_message("No videos tracked.", ephemeral=True)
        return

    msg = "\n".join(
        f"• {v['title']} ({v['video_id']})"
        for v in tracked_videos[channel_id]
    )
    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="setmilestone", description="Set milestone ping for a video")
async def setmilestone(
    interaction: discord.Interaction,
    video_id: str,
    ping_message: str
):
    channel_id = interaction.channel.id
    for v in tracked_videos.get(channel_id, []):
        if v["video_id"] == video_id:
            v["milestone_ping"] = ping_message
            await interaction.response.send_message(
                "Milestone alert set.", ephemeral=True
            )
            return

    await interaction.response.send_message("Video not found.", ephemeral=True)

@tree.command(name="listmilestones", description="List milestone alerts")
async def listmilestones(interaction: discord.Interaction):
    channel_id = interaction.channel.id
    vids = [
        v for v in tracked_videos.get(channel_id, [])
        if v["milestone_ping"]
    ]

    if not vids:
        await interaction.response.send_message("No milestone alerts.", ephemeral=True)
        return

    msg = "\n".join(
        f"• {v['title']} → {v['milestone_ping']}"
        for v in vids
    )
    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="views", description="Get current views of a video")
async def views(interaction: discord.Interaction, video_id: str):
    v = await get_views(video_id)
    if v is None:
        await interaction.response.send_message("Invalid video ID.", ephemeral=True)
        return
    await interaction.response.send_message(f"Views: **{v:,}**", ephemeral=True)

@tree.command(name="forcecheck", description="Run tracker immediately")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.send_message("Running tracker...", ephemeral=True)
    await run_tracker_for_channel(interaction.channel.id)
    await interaction.followup.send("Done.", ephemeral=True)

# =========================================================
# READY
# =========================================================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await tree.sync()

    # Immediate catch-up run
    for cid in tracked_videos:
        await run_tracker_for_channel(cid)

    asyncio.create_task(kst_scheduler())

bot.run(DISCORD_TOKEN)
