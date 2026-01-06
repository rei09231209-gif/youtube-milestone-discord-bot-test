import discord
from discord.ext import tasks, commands
from discord import app_commands
import aiohttp
import os
import json
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
import pytz
import asyncio

# ========================================
# ENV
# ========================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
PORT = int(os.getenv("PORT", 8080))
DATA_FILE = os.getenv("DATA_FILE", "yt_data.json")

if not BOT_TOKEN or not YOUTUBE_API_KEY:
    raise ValueError("❌ Missing BOT_TOKEN or YOUTUBE_API_KEY")

# ========================================
# TIME (KST)
# ========================================
KST = pytz.timezone("Asia/Seoul")

def now_kst():
    return datetime.now(KST)

TRACK_HOURS = [0, 12, 17]  # 12AM, 12PM, 5PM KST

# ========================================
# JSON PERSISTENCE (RENDER SAFE)
# ========================================
data = {
    "videos": {},
    "milestones": {},
    "intervals": {},
    "upcoming_alerts": {}
}

def ensure_data_structure():
    """Ensure all data keys exist"""
    data.setdefault("videos", {})
    data.setdefault("milestones", {})
    data.setdefault("intervals", {})
    data.setdefault("upcoming_alerts", {})

def load_data():
    global data
    ensure_data_structure()
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                loaded = json.load(f)
                data.update(loaded)
        ensure_data_structure()
        print(f"✅ Loaded {len(data.get('videos', {}))} videos from {DATA_FILE}")
    except Exception as e:
        print(f"⚠️ Data load failed: {e}")

def save_data():
    try:
        ensure_data_structure()
        os.makedirs(os.path.dirname(DATA_FILE) if os.path.dirname(DATA_FILE) else '.', exist_ok=True)
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"❌ Data save failed: {e}")

# Auto-save every 30 seconds
async def periodic_save():
    while True:
        try:
            await asyncio.sleep(30)
            if 'bot' in globals() and bot.is_closed():
                break
            save_data()
        except:
            break

# ========================================
# YOUTUBE API
# ========================================
async def fetch_views(video_id):
    """Fetch views with retries."""
    url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
    
    for attempt in range(3):
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as r:
                    if r.status != 200:
                        await asyncio.sleep(1)
                        continue
                    resp_data = await r.json()
                    if not resp_data.get("items"):
                        return None
                    return int(resp_data["items"][0]["statistics"]["viewCount"])
        except Exception:
            await asyncio.sleep(1 + attempt * 0.5)
    return None

# ========================================
# FLASK KEEPALIVE
# ========================================
app = Flask(__name__)

@app.route("/")
def home():
    return {"status": "alive", "time": now_kst().isoformat(), "videos": len(data.get("videos", {}))}

@app.route("/health")
def health():
    return {"db": "json", "videos": len(data.get("videos", {})), "status": "running"}

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False)

# ========================================
# DISCORD BOT
# ========================================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

def ensure_video_exists(video_id, guild_id, title="", channel_id=0, alert_channel=0):
    """Ensure video entry exists in data."""
    ensure_data_structure()
    key = f"{video_id}_{guild_id}"
    if key not in data["videos"]:
        data["videos"][key] = {
            "video_id": video_id,
            "title": title or video_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "alert_channel": alert_channel
        }
        data["milestones"][video_id] = data["milestones"].get(video_id, {"last_million": 0, "ping": ""})
        data["intervals"][video_id] = data["intervals"].get(video_id, {"hours": 0, "next_run": "", "last_views": 0, "last_interval_views": 0})
        save_data()

# ========================================
# 🔧 FIXED KST TRACKER (Exact hour only)
# ========================================
@tasks.loop(minutes=1)
async def kst_tracker():
    try:
        now = now_kst()
        # ✅ FIXED: Only exact hour (minute == 0), no duplicates
        if now.hour not in TRACK_HOURS or now.minute != 0:
            return
        
        print(f"🔄 KST Check: {now.strftime('%H:%M')}")
        
        for key, video in data.get("videos", {}).items():
            vid = video["video_id"]
            title = video["title"]
            alert_ch = video["alert_channel"]
            
            views = await fetch_views(vid)
            if views is None:
                continue

            # ✅ FIXED: Sync BOTH view counters
            old_views = data["intervals"].get(vid, {}).get("last_views", 0)
            net = f"(+{views-old_views:,})" if old_views else ""

            channel = bot.get_channel(alert_ch)
            if channel:
                await channel.send(
                    f"📅 **{now.strftime('%Y-%m-%d %H:%M KST')}**\n"
                    f"👀 **{title}** — {views:,} views {net}"
                )

            # ✅ FIXED: Update ALL view fields consistently
            data.setdefault("intervals", {})[vid] = data["intervals"].get(vid, {})
            data["intervals"][vid]["last_views"] = views
            data["intervals"][vid]["last_interval_views"] = views  # Sync with interval tracker
            data["intervals"][vid]["next_run"] = now.isoformat()
            save_data()
            
            # Check milestones
            million = views // 1_000_000
            milestone = data["milestones"].get(vid, {})
            if million > milestone.get("last_million", 0) and milestone.get("ping"):
                try:
                    ch_id, ping_msg = milestone["ping"].split("|", 1)
                    mil_channel = bot.get_channel(int(ch_id))
                except:
                    mil_channel = channel
                    ping_msg = milestone["ping"]
                
                if mil_channel:
                    await mil_channel.send(
                        f"🏆 **{title}** crossed **{million}M views**!\n{ping_msg}"
                    )
                
                data["milestones"][vid]["last_million"] = million
                save_data()

    except Exception as e:
        print(f"❌ KST Tracker Error: {e}")

# ========================================
# 🔧 FIXED INTERVAL TRACKER (Precise timing)
# ========================================
@tasks.loop(minutes=5)
async def tracking_loop():
    try:
        now = now_kst()
        ensure_data_structure()
        
        # ✅ FIXED: Safe iteration (list copy prevents runtime errors)
        for vid, interval_data in list(data["intervals"].items()):
            hours = interval_data.get("hours", 0)
            if hours <= 0:
                continue
                
            # ✅ FIXED: Precise next_run calculation from LAST run
            try:
                last_run_str = interval_data.get("next_run", "")
                if not last_run_str:
                    raise ValueError("No last_run")
                    
                last_run = datetime.fromisoformat(last_run_str).replace(tzinfo=KST)
                expected_next = last_run + timedelta(hours=hours)
            except:
                # Reset corrupted timer
                expected_next = now + timedelta(hours=hours)

            if now < expected_next:
                continue

            video = next((v for v in data["videos"].values() if v["video_id"] == vid), None)
            if not video:
                continue

            title = video["title"]
            ch_id = video["channel_id"]
            channel = bot.get_channel(ch_id)
            if not channel:
                continue

            views = await fetch_views(vid)
            if views is None:
                continue

            net = views - interval_data.get("last_interval_views", 0)
            
            # ✅ FIXED: Always set EXACTLY N hours from NOW
            next_time = now + timedelta(hours=hours)

            await channel.send(
                f"⏱️ **{title}** Interval Update\n"
                f"📊 {views:,} views (+{net:,})\n"
                f"⏳ Next: {hours}hrs ({next_time.strftime('%H:%M KST')})"
            )

            # ✅ FIXED: Consistent state update
            data["intervals"][vid] = {
                "hours": hours,
                "next_run": next_time.isoformat(),
                "last_views": views,
                "last_interval_views": views
            }
            save_data()

    except Exception as e:
        print(f"❌ Interval Error: {e}")

# ========================================
# ALL 15 SLASH COMMANDS (UNCHANGED - WORKING)
# ========================================
@bot.tree.command(name="addvideo", description="Add video to track")
@app_commands.describe(video_id="YouTube video ID", title="Video title")
async def addvideo(interaction: discord.Interaction, video_id: str, title: str):
    key = f"{video_id}_{interaction.guild.id}"
    if key in data.get("videos", {}):
        await interaction.response.send_message(f"⚠️ **{title}** already tracked here!")
        return
    ensure_video_exists(video_id, interaction.guild.id, title, interaction.channel.id, interaction.channel.id)
    await interaction.response.send_message(f"✅ Tracking **{title}** in <#{interaction.channel.id}>")

@bot.tree.command(name="removevideo", description="Remove video")
@app_commands.describe(video_id="YouTube video ID")
async def removevideo(interaction: discord.Interaction, video_id: str):
    guild_id = interaction.guild.id
    before_count = len(data.get("videos", {}))
    data["videos"] = {k: v for k, v in data.get("videos", {}).items() 
                     if not (v["video_id"] == video_id and v["guild_id"] == guild_id)}
    
    guild_videos = [v["video_id"] for v in data["videos"].values() if v["guild_id"] == guild_id]
    if video_id not in guild_videos:
        data["milestones"].pop(video_id, None)
        data["intervals"].pop(video_id, None)
    
    save_data()
    after_count = len(data.get("videos", {}))
    await interaction.response.send_message(f"🗑️ Video removed ({before_count-after_count} deleted)")

@bot.tree.command(name="listvideos", description="Videos in this channel")
async def listvideos(interaction: discord.Interaction):
    channel_videos = [v["title"] for v in data.get("videos", {}).values() if v["channel_id"] == interaction.channel.id]
    if not channel_videos:
        await interaction.response.send_message("📭 No videos here")
    else:
        await interaction.response.send_message("\n".join(f"• {t}" for t in channel_videos))

@bot.tree.command(name="serverlist", description="All server videos")
async def serverlist(interaction: discord.Interaction):
    server_videos = [v["title"] for v in data.get("videos", {}).values() if v["guild_id"] == interaction.guild.id]
    if not server_videos:
        await interaction.response.send_message("📭 No server videos")
    else:
        await interaction.response.send_message("\n".join(f"• {t}" for t in server_videos))

@bot.tree.command(name="forcecheck", description="Force check channel videos")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()
    channel_videos = [v for v in data.get("videos", {}).values() if v["channel_id"] == interaction.channel.id]
    
    if not channel_videos:
        return await interaction.followup.send("⚠️ No videos")
    
    for video in channel_videos:
        title = video["title"]
        vid = video["video_id"]
        views = await fetch_views(vid)
        if views:
            # ✅ FIXED: Sync both counters
            data.setdefault("intervals", {})[vid] = data["intervals"].get(vid, {})
            data["intervals"][vid]["last_views"] = views
            data["intervals"][vid]["last_interval_views"] = views
            save_data()
            old_views = views - (views - data["intervals"][vid].get("last_views", 0))  # Just for display
            await interaction.followup.send(f"📊 **{title}**: {views:,}")
        else:
            await interaction.followup.send(f"❌ **{title}**: fetch failed")

@bot.tree.command(name="views", description="Single video views")
@app_commands.describe(video_id="YouTube video ID")
async def views(interaction: discord.Interaction, video_id: str):
    v = await fetch_views(video_id)
    if v:
        await interaction.response.send_message(f"📊 {v:,} views")
    else:
        await interaction.response.send_message("❌ Fetch failed")

@bot.tree.command(name="viewsall", description="All server videos")
async def viewsall(interaction: discord.Interaction):
    await interaction.response.defer()
    server_videos = [v for v in data.get("videos", {}).values() if v["guild_id"] == interaction.guild.id]
    
    if not server_videos:
        return await interaction.followup.send("⚠️ No videos")
    
    for video in server_videos:
        title = video["title"]
        vid = video["video_id"]
        views = await fetch_views(vid)
        if views:
            data.setdefault("intervals", {})[vid] = data["intervals"].get(vid, {})
            data["intervals"][vid]["last_views"] = views
            data["intervals"][vid]["last_interval_views"] = views
            save_data()
            await interaction.followup.send(f"📊 **{title}**: {views:,}")
        else:
            await interaction.followup.send(f"❌ **{title}**: fetch failed")

@bot.tree.command(name="setmilestone", description="Milestone alerts")
@app_commands.describe(video_id="Video ID", channel="Alert channel", ping="Ping message")
async def setmilestone(interaction: discord.Interaction, video_id: str, channel: discord.TextChannel, ping: str = ""):
    data.setdefault("milestones", {})[video_id] = {"last_million": data["milestones"].get(video_id, {}).get("last_million", 0), "ping": f"{channel.id}|{ping}"}
    save_data()
    await interaction.response.send_message(f"🏆 Alerts set for <#{channel.id}> {'<@&role>' if ping else ''}")

@bot.tree.command(name="reachedmilestones", description="Show hit milestones")
async def reachedmilestones(interaction: discord.Interaction):
    await interaction.response.defer()
    lines = []
    server_videos = [v for v in data.get("videos", {}).values() if v["guild_id"] == interaction.guild.id]
    
    for video in server_videos:
        vid = video["video_id"]
        milestone = data.get("milestones", {}).get(vid, {})
        if milestone.get("last_million", 0) > 0:
            lines.append(f"🏆 **{video['title']}**: {milestone['last_million']}M")
    
    if not lines:
        await interaction.followup.send("📭 No milestones yet")
    else:
        await interaction.followup.send("\n".join(lines))

@bot.tree.command(name="removemilestones", description="Clear milestone alerts")
@app_commands.describe(video_id="Video ID")
async def removemilestones(interaction: discord.Interaction, video_id: str):
    if video_id in data.get("milestones", {}):
        data["milestones"][video_id] = {"last_million": data["milestones"][video_id].get("last_million", 0), "ping": ""}
        save_data()
    await interaction.response.send_message("❌ Alerts cleared")

@bot.tree.command(name="setinterval", description="Custom update interval")
@app_commands.describe(video_id="Video ID", hours="Hours between checks")
async def setinterval(interaction: discord.Interaction, video_id: str, hours: float):
    ensure_video_exists(video_id, interaction.guild.id)
    now = now_kst()
    data["intervals"][video_id] = {
        "hours": hours,
        "next_run": (now + timedelta(hours=hours)).isoformat(),
        "last_views": 0,
        "last_interval_views": 0
    }
    save_data()
    await interaction.response.send_message(f"⏱️ **{hours}hr** intervals set for `{video_id}`\nNext: ~{now + timedelta(hours=hours):%H:%M KST}")

@bot.tree.command(name="disableinterval", description="Stop interval updates")
@app_commands.describe(video_id="Video ID")
async def disableinterval(interaction: discord.Interaction, video_id: str):
    data["intervals"].pop(video_id, None)
    save_data()
    await interaction.response.send_message("⏹️ Intervals stopped")

@bot.tree.command(name="setupcomingmilestonesalert", description="Upcoming summary")
@app_commands.describe(channel="Summary channel", ping="Ping message")
async def setupcomingmilestonesalert(interaction: discord.Interaction, channel: discord.TextChannel, ping: str = ""):
    data["upcoming_alerts"][str(interaction.guild.id)] = {
        "channel_id": channel.id,
        "ping": ping
    }
    save_data()
    await interaction.response.send_message("📌 Summary alerts configured")

@bot.tree.command(name="upcoming", description="Videos near milestones")
async def upcoming(interaction: discord.Interaction):
    await interaction.response.defer()
    lines = []
    server_videos = [v for v in data.get("videos", {}).values() if v["guild_id"] == interaction.guild.id]
    
    for video in server_videos:
        views = await fetch_views(video["video_id"])
        if views:
            next_m = ((views // 1_000_000) + 1) * 1_000_000
            diff = next_m - views
            if diff <= 100_000:
                lines.append(f"⏳ **{video['title']}**: {diff:,} to {next_m:,}")
    
    if not lines:
        await interaction.followup.send("📭 Nothing near milestones")
    else:
        await interaction.followup.send("\n".join(lines))

@bot.tree.command(name="botcheck", description="Bot status")
async def botcheck(interaction: discord.Interaction):
    now = now_kst()
    intervals_active = sum(1 for i in data.get("intervals", {}).values() if i.get("hours", 0) > 0)
    await interaction.response.send_message(
        f"✅ **KST**: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📊 **{len(data.get('videos', {}))}** videos tracked\n"
        f"⏱️ **{intervals_active}** intervals active\n"
        f"💾 Data saved: {os.path.exists(DATA_FILE)}"
    )

# ========================================
# BOT EVENTS
# ========================================
@bot.event
async def on_ready():
    load_data()
    
    print(f"🚀 {bot.user} online - KST: {now_kst().strftime('%H:%M:%S')}")
    print(f"💾 Loaded {len(data.get('videos', {}))} videos")
    
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        print(f"❌ Sync error: {e}")
    
    kst_tracker.start()
    tracking_loop.start()
    print("⏱️ Trackers started (KST: exact hour | Intervals: precise)")
    
    Thread(target=run_flask, daemon=True).start()
    print("🌐 Flask started")
    
    bot.loop.create_task(periodic_save())

@bot.event
async def on_error(event, *args, **kwargs):
    print(f"❌ Error in {event}: {args}")

# ========================================
# START
# ========================================
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
