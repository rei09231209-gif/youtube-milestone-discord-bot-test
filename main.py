import os
import asyncio
import json
import time
from datetime import datetime, timedelta
import pytz
import aiohttp
from flask import Flask
from threading import Thread

from discord.ext import commands, tasks
import discord

# =====================================================
#                 ENVIRONMENT VARIABLES
# =====================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

DAILY_UPDATE_TIMES = ["00:00", "12:00", "17:00"]  # EXACT KST TIMES YOU WANTED
KST = pytz.timezone("Asia/Seoul")

# =====================================================
#                     BOT INITIALIZE
# =====================================================

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = False

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# =====================================================
#                      DATABASE
# =====================================================

DB_FILE = "database.json"

def load_db():
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, "w") as f:
            json.dump({"videos": {}, "channels": {}, "milestones": {}}, f)
    with open(DB_FILE, "r") as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

db = load_db()

# =====================================================
#               KEEPALIVE (port 8080)
# =====================================================

app = Flask("keepalive")

@app.route("/")
def home():
    return "Bot is running"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    thread = Thread(target=run_flask)
    thread.daemon = True
    thread.start()

keep_alive()

# =====================================================
#               YOUTUBE API REQUEST
# =====================================================

async def get_video_stats(video_id):
    url = (
        f"https://www.googleapis.com/youtube/v3/videos"
        f"?part=statistics,snippet&id={video_id}&key={YOUTUBE_API_KEY}"
    )

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as r:
            if r.status != 200:
                return None
            data = await r.json()

    if "items" not in data or len(data["items"]) == 0:
        return None

    item = data["items"][0]
    title = item["snippet"]["title"]
    views = int(item["statistics"]["viewCount"])
    return {"title": title, "views": views}

# =====================================================
#                   MILESTONES
# =====================================================

def next_milestone(view_count):
    current_m = view_count // 1_000_000
    next_m = (current_m + 1) * 1_000_000
    return next_m

def upcoming_distance(view_count):
    return next_milestone(view_count) - view_count

# =====================================================
#                 TRACKING ENGINE
# =====================================================

DEFAULT_INTERVAL = 300   # 5 minutes
tracking_interval = DEFAULT_INTERVAL


async def process_video(video_id):
    stats = await get_video_stats(video_id)
    if not stats:
        return None

    title = stats["title"]
    views = stats["views"]

    if video_id not in db["videos"]:
        db["videos"][video_id] = {
            "title": title,
            "last_view_count": views,
            "milestone": (views // 1_000_000) * 1_000_000
        }
        save_db(db)
        return None

    v = db["videos"][video_id]
    old_m = v["milestone"]

    event = {
        "video_id": video_id,
        "title": title,
        "views": views,
        "milestone_hit": None
    }

    # milestone hit (YOUR RULE: even if early like 12.1M)
    if views >= old_m + 1_000_000:
        new_m = old_m + 1_000_000
        event["milestone_hit"] = new_m

        if video_id not in db["milestones"]:
            db["milestones"][video_id] = {"history": []}

        db["milestones"][video_id]["history"].append({
            "timestamp": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
            "milestone": new_m,
            "views": views
        })

        v["milestone"] = new_m

    v["last_view_count"] = views
    save_db(db)

    return event


async def send_tracking_messages(events):
    for event in events:
        video_id = event["video_id"]
        title = event["title"]
        views = event["views"]
        hit = event["milestone_hit"]

        for cid, vids in db["channels"].items():
            if video_id not in vids:
                continue

            channel = bot.get_channel(int(cid))
            if not channel:
                continue

            if hit:
                await channel.send(
                    f"Milestone reached for **{title}**\n"
                    f"Milestone: {hit}\n"
                    f"Current views: {views}"
                )


@tasks.loop(seconds=5)
async def scheduler_loop():
    """
    Runs every 5 seconds and checks:
    - daily fixed-time summaries
    - interval tracking
    """

    now = datetime.now(KST)
    t_str = now.strftime("%H:%M")

    # DAILY EXACT KST UPDATES (00:00, 12:00, 17:00)
    if t_str in DAILY_UPDATE_TIMES:
        if getattr(bot, "_last_daily", None) != t_str:
            bot._last_daily = t_str
            await run_daily_update()

    # interval tracking
    if not hasattr(bot, "_tick"):
        bot._tick = 0

    bot._tick += 5
    if bot._tick >= tracking_interval:
        bot._tick = 0
        await run_full_tracking()


async def run_full_tracking():
    events = []
    for vid in list(db["videos"].keys()):
        event = await process_video(vid)
        if event:
            events.append(event)

    if events:
        await send_tracking_messages(events)


async def run_daily_update():
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    for cid, vids in db["channels"].items():
        channel = bot.get_channel(int(cid))
        if not channel:
            continue

        output = [f"Daily view summary for {now} (KST)"]
        for vid in vids:
            if vid not in db["videos"]:
                continue
            v = db["videos"][vid]
            output.append(f"{v['title']}: {v['last_view_count']}")

        await channel.send("\n".join(output))

# =====================================================
#                   BOT READY
# =====================================================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    scheduler_loop.start()

# =====================================================
#                     COMMANDS
# =====================================================


# -----------------------------------------------------
#        /addvideo   (anyone can use — your choice)
# -----------------------------------------------------

@bot.slash_command(name="addvideo", description="Assign a video to track in this channel.")
async def addvideo(ctx, video_id: str):

    stats = await get_video_stats(video_id)
    if not stats:
        return await ctx.respond("Invalid video ID.", ephemeral=True)

    cid = str(ctx.channel.id)

    # enforce one-video-per-channel
    db["channels"][cid] = [video_id]

    if video_id not in db["videos"]:
        db["videos"][video_id] = {
            "title": stats["title"],
            "last_view_count": stats["views"],
            "milestone": (stats["views"] // 1_000_000) * 1_000_000
        }

    save_db(db)

    await ctx.respond(
        f"Now tracking **{stats['title']}** in this channel.\n"
        f"Current views: {stats['views']}"
    )

# -----------------------------------------------------
#              /removevideo
# -----------------------------------------------------

@bot.slash_command(name="removevideo", description="Remove this channel's tracked video.")
async def removevideo(ctx):
    cid = str(ctx.channel.id)

    if cid not in db["channels"] or len(db["channels"][cid]) == 0:
        return await ctx.respond("This channel is not tracking any video.", ephemeral=True)

    db["channels"][cid] = []
    save_db(db)

    await ctx.respond("Removed tracking for this channel.")


# -----------------------------------------------------
#              /forcecheck
# -----------------------------------------------------

@bot.slash_command(name="forcecheck", description="Force check all videos now.")
async def forcecheck(ctx):
    await ctx.respond("Checking now...")

    events = []
    for vid in db["videos"].keys():
        event = await process_video(vid)
        if event:
            events.append(event)

    if not events:
        return await ctx.channel.send("No updates found.")

    await send_tracking_messages(events)


# -----------------------------------------------------
#           /upcomingmilestones (no loops)
# -----------------------------------------------------

@bot.slash_command(name="upcomingmilestones", description="Show the next milestone for this channel's video.")
async def upcomingmilestones(ctx):
    cid = str(ctx.channel.id)

    if cid not in db["channels"] or len(db["channels"][cid]) == 0:
        return await ctx.respond("This channel is not tracking any video.", ephemeral=True)

    vid = db["channels"][cid][0]

    if vid not in db["videos"]:
        return await ctx.respond("Video missing from database.", ephemeral=True)

    v = db["videos"][vid]
    title = v["title"]
    views = v["last_view_count"]

    next_m = next_milestone(views)
    dist = next_m - views

    if dist <= 0:
        return await ctx.respond("No upcoming milestones.", ephemeral=True)

    await ctx.respond(
        f"Upcoming milestone for **{title}**:\n"
        f"Next: {next_m}\n"
        f"Views remaining: {dist}"
    )


# -----------------------------------------------------
#        /reachedmilestones   (last 24 hours only)
# -----------------------------------------------------

@bot.slash_command(name="reachedmilestones", description="List milestones hit in the last 24 hours.")
async def reachedmilestones(ctx):
    cid = str(ctx.channel.id)

    if cid not in db["channels"] or len(db["channels"][cid]) == 0:
        return await ctx.respond("This channel has no tracked video.", ephemeral=True)

    vid = db["channels"][cid][0]

    if vid not in db["milestones"]:
        return await ctx.respond("No milestones found.", ephemeral=True)

    now = datetime.now(KST)
    cutoff = now - timedelta(hours=24)

    lines = []
    for entry in db["milestones"][vid]["history"]:
        ts = datetime.strptime(entry["timestamp"], "%Y-%m-%d %H:%M:%S")
        ts = KST.localize(ts)

        if ts >= cutoff:
            lines.append(
                f"{entry['timestamp']} | {entry['milestone']} | {entry['views']} views"
            )

    if not lines:
        return await ctx.respond("No milestones reached in the last 24 hours.")

    await ctx.respond("\n".join(lines))


# -----------------------------------------------------
#               /setinterval
# -----------------------------------------------------

@bot.slash_command(name="setinterval", description="Set tracking interval in minutes.")
async def setinterval(ctx, minutes: int):
    global tracking_interval
    tracking_interval = minutes * 60
    await ctx.respond(f"Tracking interval updated to {minutes} minutes.")


# -----------------------------------------------------
#                  /botstats
# -----------------------------------------------------

@bot.slash_command(name="botstats", description="Show bot statistics.")
async def botstats(ctx):
    tv = len(db["videos"])
    tc = len(db["channels"])
    await ctx.respond(f"Tracked videos: {tv}\nTracking channels: {tc}")


# =====================================================
#                   BOT START
# =====================================================

bot.run(DISCORD_TOKEN)
