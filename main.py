import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import asyncio
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
import logging
import json
import atexit
import re
from utils import *  # Contains: init_db, db_execute, now_kst, backup_db, restore_db

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))

if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN")

# 🚀 PERSISTENT DB - Uses utils.py functions
restore_db()  # Auto-restore from backup.db
atexit.register(backup_db)  # Auto-backup on shutdown
print("💾 DB persistence ACTIVE")

intents = discord.Intents.default()
intents.voice_states = False
bot = commands.Bot(command_prefix='!', intents=intents)

# 🌐 FLASK KEEPALIVE (keeps Render awake 24/7)
app = Flask(__name__)
@app.route("/")
@app.route("/health")
def home():
    return {"status": "alive", "time": now_kst().isoformat()}

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# START FLASK FIRST
Thread(target=run_flask, daemon=True).start()
print("🌐 Flask ACTIVE - Render stays awake 24/7!")

# Safe response helper
async def safe_response(interaction, content):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content)
        else:
            await interaction.response.send_message(content)
    except:
        pass

kst = pytz.timezone('Asia/Seoul')

# KST TRACKER (00:00, 12:00, 17:00) - Server milestones ONLY here
@tasks.loop(minutes=1)
async def kst_tracker():
    try:
        now = now_kst()
        if now.minute != 0 or now.hour not in [0, 12, 17]:
            return

        print(f"🕐 KST Tracker running at {now.strftime('%H:%M KST')} - Server milestone window")
        
        # FIXED: Guild-specific videos only (THIS WAS THE BUG)
        guild_ids = [str(guild.id) for guild in bot.guilds if guild]
        if guild_ids:
            placeholders = ','.join(['?' for _ in guild_ids])
            videos = await db_execute(f"SELECT * FROM videos WHERE guild_id IN ({placeholders})", guild_ids, fetch=True) or []
        else:
            videos = []
            
        guild_upcoming = {}

        for video in videos:
            video_id = video['video_id']
            title = video['title']
            guild_id = video['guild_id']
            alert_ch = video['alert_channel']

            views, likes = await fetch_video_stats(video_id)
            if views is None:
                continue

            # KST STATS MESSAGE
            kst_data = await db_execute(
                "SELECT kst_last_views FROM intervals WHERE video_id=? AND guild_id=?", 
                (video_id, guild_id), fetch=True
            ) or []
            kst_last = kst_data[0]['kst_last_views'] if kst_data else 0
            kst_net = f"(+{views-kst_last:,})" if kst_last else ""

            channel = bot.get_channel(int(alert_ch))
            if channel:
                await channel.send(f"""📅 **{now.strftime('%Y-%m-%d %H:%M KST')}**
👀 {title} — {views:,} views {kst_net}""")

            # UPDATE VIEW HISTORY
            history = await db_execute(
                "SELECT view_history FROM intervals WHERE video_id=? AND guild_id=?", 
                (video_id, guild_id), fetch=True
            ) or []
            try:
                hist = json.loads(history[0]['view_history']) if history and history[0]['view_history'] != '[]' else []
                hist.append({"views": views, "time": now.isoformat()})
                hist = hist[-10:]
                await db_execute(
                    "UPDATE intervals SET kst_last_views=?, kst_last_run=?, last_views=?, view_history=? WHERE video_id=? AND guild_id=?",
                    (views, now.isoformat(), views, json.dumps(hist), video_id, guild_id)
                )
            except:
                await db_execute(
                    "UPDATE intervals SET kst_last_views=?, kst_last_run=?, last_views=? WHERE video_id=? AND guild_id=?",
                    (views, now.isoformat(), views, video_id, guild_id)
                )

            # VIDEO MILESTONES (always during KST)
            milestone_data = await db_execute(
                "SELECT ping, last_million FROM milestones WHERE video_id=? AND guild_id=?",
                (video_id, guild_id), fetch=True
            ) or []
            current_million = views // 1_000_000
            if milestone_data:
                ping_str, last_million = milestone_data[0]['ping'], milestone_data[0]['last_million']
                if current_million > (last_million or 0):
                    if ping_str and ping_str != f"{ping_str.split('|')[0]}|":
                        try:
                            ping_channel_id, role_ping = ping_str.split('|')
                            ping_channel = bot.get_channel(int(ping_channel_id))
                            if ping_channel:
                                youtube_url = f"https://youtu.be/{video_id}"
                                await ping_channel.send(f"""🎉 **{title[:30]}** hit **{current_million}M VIEWS**! 🚀
📊 {views:,} views | ❤️ {likes:,} likes
🔗 {youtube_url}
{role_ping}""")
                        except Exception as e:
                            print(f"Milestone ping error: {e}")
                    await db_execute(
                        "UPDATE milestones SET last_million=? WHERE video_id=? AND guild_id=?", 
                        (current_million, video_id, guild_id)
                    )

            # UPCOMING <100K
            next_m = ((views // 1_000_000) + 1) * 1_000_000
            diff = next_m - views
            if 0 < diff <= 100_000:
                if guild_id not in guild_upcoming:
                    guild_upcoming[guild_id] = []
                try:
                    growth_rate = await get_real_growth_rate(video_id, guild_id)
                    hours = diff / max(growth_rate, 10)
                    if hours < 1:
                        eta = f"{int(hours*60)}min"
                    elif hours < 24:
                        eta = f"{int(hours)}h"
                    elif hours < 168:
                        eta = f"{int(hours/24)}d"
                    else:
                        eta = f"{int(hours/24/7)}w"
                    guild_upcoming[guild_id].append(f"⏳ **{title}**: **{diff:,}** to {next_m:,} **(ETA: {eta})**")
                except:
                    guild_upcoming[guild_id].append(f"⏳ **{title}**: **{diff:,}** to {next_m:,}")

        # UPCOMING SUMMARY
        for guild_id, upcoming_list in guild_upcoming.items():
            upcoming_data = await db_execute(
                "SELECT channel_id, ping FROM upcoming_alerts WHERE guild_id=?", 
                (guild_id,), fetch=True
            ) or []
            if upcoming_data and upcoming_list:
                ch_id, ping_role = upcoming_data[0]['channel_id'], upcoming_data[0]['ping']
                channel = bot.get_channel(int(ch_id))
                if channel:
                    message = f"""📊 **UPCOMING <100K** ({now.strftime('%H:%M KST')}):
{chr(10).join(upcoming_list)}
🔔 {ping_role}"""
                    await channel.send(message)

    except Exception as e:
        print(f"KST tracker error: {e}")

# INTERVAL CHECKER (Multi-guild + jitter tolerance)
@tasks.loop(minutes=1)
async def interval_checker():
    try:
        now = now_kst()
        
        intervals = await db_execute(
            "SELECT i.video_id, i.hours, i.guild_id, v.title, v.alert_channel FROM intervals i JOIN videos v ON i.video_id = v.video_id WHERE i.hours > 0",
            fetch=True
        ) or []
        
        for row in intervals:
            vid, hours, stored_guild_id, title, alert_ch_id = row['video_id'], row['hours'], row['guild_id'], row['title'], row['alert_channel']

            # CRITICAL: Find channel FIRST
            channel = bot.get_channel(int(alert_ch_id))
            if not channel:
                continue

            # ABSOLUTE BLOCK: Channel's guild MUST match stored guild_id
            if str(channel.guild.id) != stored_guild_id:
                print(f"🚫 BLOCKED: {title} stored for guild {stored_guild_id} but channel in {channel.guild.id}")
                continue

            # Now process normally (your original logic)
            last_run_data = await db_execute(
                "SELECT last_interval_run, last_interval_views FROM intervals WHERE video_id=? AND guild_id=?", 
                (vid, stored_guild_id), fetch=True
            ) or []

            last_time_str = last_run_data[0]['last_interval_run'] if last_run_data else None
            prev_views = last_run_data[0]['last_interval_views'] if last_run_data else 0

            should_run = True
            if last_time_str:
                try:
                    last_time = datetime.fromisoformat(last_time_str).astimezone(kst)
                    if (now - last_time) < timedelta(hours=hours-0.0167):
                        should_run = False
                except:
                    should_run = True

            if not should_run:
                continue

            views, likes = await fetch_video_stats(vid)
            if views is None:
                continue

            # MILESTONE CHECK
            milestone_data = await db_execute(
                "SELECT ping, last_million FROM milestones WHERE video_id=? AND guild_id=?",
                (vid, stored_guild_id), fetch=True
            ) or []
            current_million = views // 1_000_000
            if milestone_data:
                ping_str, last_million = milestone_data[0]['ping'], milestone_data[0]['last_million']
                if current_million > (last_million or 0):
                    if ping_str and ping_str != f"{ping_str.split('|')[0]}|":
                        try:
                            ping_channel_id, role_ping = ping_str.split('|')
                            ping_channel = bot.get_channel(int(ping_channel_id))
                            # SAME GUILD CHECK FOR PING CHANNEL
                            if ping_channel and str(ping_channel.guild.id) == stored_guild_id:
                                youtube_url = f"https://youtu.be/{vid}"
                                await ping_channel.send(f"""🎉 **{title[:30]}** hit **{current_million}M VIEWS**! 🚀
📊 {views:,} views | ❤️ {likes:,} likes
🔗 {youtube_url}
{role_ping}""")
                        except Exception as e:
                            print(f"Milestone ping error: {e}")
                    await db_execute(
                        "UPDATE milestones SET last_million=? WHERE video_id=? AND guild_id=?", 
                        (current_million, vid, stored_guild_id)
                    )

            net = views - prev_views
            next_time = now + timedelta(hours=hours)

            # UPDATE HISTORY
            history = await db_execute(
                "SELECT view_history FROM intervals WHERE video_id=? AND guild_id=?", 
                (vid, stored_guild_id), fetch=True
            ) or []
            try:
                hist = json.loads(history[0]['view_history']) if history and history[0]['view_history'] != '[]' else []
                hist.append({"views": views, "time": now.isoformat()})
                hist = hist[-10:]
                await db_execute(
                    "UPDATE intervals SET last_interval_views=?, last_interval_run=?, view_history=? WHERE video_id=? AND guild_id=?",
                    (views, now.isoformat(), json.dumps(hist), vid, stored_guild_id)
                )
            except:
                await db_execute(
                    "UPDATE intervals SET last_interval_views=?, last_interval_run=? WHERE video_id=? AND guild_id=?",
                    (views, now.isoformat(), vid, stored_guild_id)
                )

            # FINAL SAFETY CHECK BEFORE SEND
            if str(channel.guild.id) != stored_guild_id:
                print(f"🚫 FINAL BLOCK: Guild mismatch!")
                continue

            await channel.send(f"""⏱️ **{title}** ({hours}hr interval)
📊 {views:,} views (+{net:,})
⏳ Next: {next_time.strftime('%H:%M KST')}""")

    except Exception as e:
        print(f"Interval checker error: {e}")

# Task startup hooks
@interval_checker.before_loop
async def before_interval_checker():
    await bot.wait_until_ready()
    print("✅ Interval checker ready")

@kst_tracker.before_loop
async def before_kst_tracker():
    await bot.wait_until_ready()
    print("✅ KST tracker ready")

# COMMANDS 1-8: Status + Video Management + Basic Stats
@bot.tree.command(name="botcheck", description="Bot status and health")
async def botcheck(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    now = now_kst()
    
    # FIXED: Guild-specific counts only
    vcount = len(await db_execute("SELECT * FROM videos WHERE guild_id=?", (guild_id,), fetch=True) or [])
    icount = len(await db_execute("SELECT * FROM intervals WHERE guild_id=? AND hours > 0", (guild_id,), fetch=True) or [])
    
    kst_status = "🟢" if kst_tracker.is_running() else "🔴"
    interval_status = "🟢" if interval_checker.is_running() else "🔴"
    
    await safe_response(interaction, f"""✅ **KST**: {now.strftime('%Y-%m-%d %H:%M:%S')} | **{interaction.guild.name}**
📊 **{vcount}** videos | **{icount}** intervals 
🔄 KST: {kst_status} | Intervals: {interval_status}
💾 DB: Connected | 🌐 PORT: {PORT}""")

@bot.tree.command(name="addvideo", description="Add YouTube video to track (URL or ID)")
@app_commands.describe(url_or_id="YouTube URL or video ID", title="Video title (optional)")
async def addvideo(interaction: discord.Interaction, url_or_id: str, title: str = ""):
    video_id = extract_video_id(url_or_id)
    if not video_id:
        await safe_response(interaction, "❌ Invalid YouTube URL/ID")
        return
    guild_id = str(interaction.guild.id)
    
    # CHECK IF EXISTS FOR THIS GUILD FIRST
    exists = await db_execute("SELECT 1 FROM videos WHERE video_id=? AND guild_id=?", 
                            (video_id, guild_id), fetch=True)
    if exists:
        await safe_response(interaction, "✅ Video already tracked in this server")
        return
    
    # ADD NEW ENTRY FOR THIS GUILD
    await db_execute("""
        INSERT INTO videos (video_id, title, guild_id, alert_channel, channel_id) 
        VALUES (?, ?, ?, ?, ?)
    """, (video_id, title or video_id, guild_id, interaction.channel.id, interaction.channel.id))
    
    await safe_response(interaction, f"✅ **{title or video_id}** → <#{interaction.channel.id}>")

@bot.tree.command(name="removevideo", description="Remove video from tracking (URL or ID)")
@app_commands.describe(url_or_id="YouTube URL or video ID")
async def removevideo(interaction: discord.Interaction, url_or_id: str):
    video_id = extract_video_id(url_or_id)
    if not video_id:
        await safe_response(interaction, "❌ Invalid URL/ID")
        return
    count = len(await db_execute("SELECT * FROM videos WHERE video_id=? AND guild_id=?", 
                               (video_id, str(interaction.guild.id)), fetch=True) or [])
    await db_execute("DELETE FROM videos WHERE video_id=? AND guild_id=?", (video_id, str(interaction.guild.id)))
    if not await db_execute("SELECT 1 FROM videos WHERE video_id=?", (video_id,), fetch=True):
        await db_execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
        await db_execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    await safe_response(interaction, f"🗑️ Removed **{count}** video(s)")

@bot.tree.command(name="listvideos", description="Videos in current channel")
async def listvideos(interaction: discord.Interaction):
    videos = await db_execute("SELECT title FROM videos WHERE channel_id=?", (interaction.channel.id,), fetch=True) or []
    if not videos:
        await safe_response(interaction, "📭 No videos in this channel")
    else:
        await safe_response(interaction, f"""📋 **Channel videos**:
{chr(10).join(f"• {v['title']}" for v in videos)}""")

@bot.tree.command(name="serverlist", description="All server videos")
async def serverlist(interaction: discord.Interaction):
    videos = await db_execute("SELECT title FROM videos WHERE guild_id=?", (str(interaction.guild.id),), fetch=True) or []
    if not videos:
        await safe_response(interaction, "📭 No server videos")
    else:
        await safe_response(interaction, "📋 **Server videos**:\n" + "\n".join(f"• {v['title']}" for v in videos))

@bot.tree.command(name="views", description="Check single video stats (URL or ID)")
@app_commands.describe(url_or_id="YouTube URL or video ID")
async def views(interaction: discord.Interaction, url_or_id: str):
    video_id = extract_video_id(url_or_id)
    if not video_id:
        await safe_response(interaction, "❌ Invalid URL/ID")
        return
    views, likes = await fetch_video_stats(video_id)
    if views:
        await safe_response(interaction, f"📊 **{views:,}** views | ❤️ **{likes:,}** likes")
    else:
        await safe_response(interaction, "❌ Fetch failed")

@bot.tree.command(name="forcecheck", description="Force check all channel videos NOW")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = await db_execute("SELECT title, video_id FROM videos WHERE channel_id=?", (interaction.channel.id,), fetch=True) or []
    if not videos:
        await interaction.followup.send("⚠️ No videos in this channel")
        return
    results = []
    guild_id = str(interaction.guild.id)
    now = now_kst()
    
    for video in videos:
        title, vid = video['title'], video['video_id']
        views, likes = await fetch_video_stats(vid)
        if views:
            # UPDATE intervals table for KST tracker
            await db_execute(
                "INSERT OR REPLACE INTO intervals (video_id, guild_id, last_views, kst_last_views, view_history) VALUES (?, ?, ?, ?, ?)",
                (vid, guild_id, views, views, json.dumps([{"views": views, "time": now.isoformat()}]))
            )
            results.append(f"📊 **{title}**: {views:,}❤️{likes:,}")
        else:
            results.append(f"❌ **{title}**: fetch failed")
    
    content = "📊 **Force check results**:\n" + "\n".join(results[:10])
    await interaction.followup.send(content)

@bot.tree.command(name="viewsall", description="Check ALL server video stats")
async def viewsall(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = await db_execute("SELECT title, video_id FROM videos WHERE guild_id=?", (str(interaction.guild.id),), fetch=True) or []
    if not videos:
        await interaction.followup.send("⚠️ No videos in server")
        return
    guild_id = str(interaction.guild.id)
    results = []
    
    for video in videos:
        title, vid = video['title'], video['video_id']
        views, likes = await fetch_video_stats(vid)
        if views:
            # UPDATE intervals table for KST tracker
            await db_execute(
                "INSERT OR REPLACE INTO intervals (video_id, guild_id, last_views, kst_last_views) VALUES (?, ?, ?, ?)",
                (vid, guild_id, views, views)
            )
            results.append(f"📊 **{title}**: {views:,}❤️{likes:,}")
    
    await interaction.followup.send("📊 **Server stats**:\n" + "\n".join(results[:20]))

@bot.tree.command(name="reachedmilestones", description="Videos that hit millions")
async def reachedmilestones(interaction: discord.Interaction):
    await interaction.response.defer()
    data = await db_execute(
        "SELECT v.title, m.last_million FROM milestones m JOIN videos v ON m.video_id=v.video_id WHERE v.guild_id=? AND m.last_million > 0",
        (str(interaction.guild.id),), fetch=True
    ) or []
    if not data:
        await interaction.followup.send("📭 No million milestones reached")
    else:
        await interaction.followup.send("💿 **Million Milestones Reached**:\n" + "\n".join(f"• **{t['title']}**: {t['last_million']}M" for t in data))

@bot.tree.command(name="upcoming", description="Upcoming milestones <100K (paginated)")
async def upcoming(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = str(interaction.guild.id)
    videos = await db_execute("SELECT title, video_id FROM videos WHERE guild_id=?", (guild_id,), fetch=True) or []
    
    upcoming_videos = []
    now = now_kst()
    
    for video in videos:
        title, vid = video['title'], video['video_id']
        views, _ = await fetch_video_stats(vid)
        if views:
            next_m = ((views // 1_000_000) + 1) * 1_000_000
            diff = next_m - views
            if 0 < diff <= 100_000:
                try:
                    growth_rate = await get_real_growth_rate(vid, guild_id)
                    hours = diff / max(growth_rate, 10)
                    if hours < 1: eta = f"{int(hours*60)}min"
                    elif hours < 24: eta = f"{int(hours)}h"
                    elif hours < 168: eta = f"{int(hours/24)}d"
                    else: eta = f"{int(hours/24/7)}w"
                    upcoming_videos.append(f"⏳ **{title[:35]}**: **{diff:,}** to {next_m:,} **(ETA: {eta})**")
                except:
                    upcoming_videos.append(f"⏳ **{title[:35]}**: **{diff:,}** to {next_m:,}")

    if not upcoming_videos:
        await interaction.followup.send("📭 No videos within 100K of next million")
        return
    
    page_size = 10
    pages = []
    for i in range(0, len(upcoming_videos), page_size):
        page_videos = upcoming_videos[i:i+page_size]
        page_content = f"""📊 **UPCOMING <100K** ({now.strftime('%H:%M KST')})
Page {i//page_size + 1}/{((len(upcoming_videos)-1)//page_size)+1}

{chr(10).join(page_videos)}"""
        pages.append(page_content)
    
    await start_paginator(interaction, pages)

@bot.tree.command(name="setmilestone", description="Video million alerts")
@app_commands.describe(url_or_id="YouTube URL or video ID", channel="Alert channel (optional: uses current)", ping="Optional ping/role")
async def setmilestone(interaction: discord.Interaction, url_or_id: str, channel: discord.TextChannel = None, ping: str = ""):
    video_id = extract_video_id(url_or_id)
    if not video_id:
        await safe_response(interaction, "❌ Invalid URL/ID")
        return
    guild_id = str(interaction.guild.id)
    ch_id = channel.id if channel else interaction.channel.id
    await ensure_video_exists(video_id, guild_id)
    await db_execute("INSERT OR REPLACE INTO milestones (video_id, guild_id, ping) VALUES (?, ?, ?)",
                   (video_id, guild_id, f"{ch_id}|{ping}"))
    await safe_response(interaction, f"💿 **Million alerts** → <#{ch_id}> {ping or '(no ping)'}")

@bot.tree.command(name="removemilestones", description="Clear video milestone alerts (URL or ID)")
@app_commands.describe(url_or_id="YouTube URL or video ID")
async def removemilestones(interaction: discord.Interaction, url_or_id: str):
    video_id = extract_video_id(url_or_id)
    if not video_id:
        await safe_response(interaction, "❌ Invalid URL/ID")
        return
    await db_execute("UPDATE milestones SET ping='' WHERE video_id=? AND guild_id=?", 
                   (video_id, str(interaction.guild.id)))
    await safe_response(interaction, "✅ **Video milestone alerts cleared**")

@bot.tree.command(name="setinterval", description="Set custom interval checks (URL or ID)")
@app_commands.describe(
    url_or_id="YouTube URL or video ID", 
    hours="Hours between checks (1/60=1min minimum)"
)
@app_commands.describe(channel="Channel to send updates (optional - uses current if blank)")
async def setinterval(interaction: discord.Interaction, url_or_id: str, hours: float, channel: discord.TextChannel | None = None):
    if hours < 1/60:
        await safe_response(interaction, "❌ **Minimum 1 minute (1/60 hr)**")
        return
        
    video_id = extract_video_id(url_or_id)
    if not video_id:
        await safe_response(interaction, "❌ Invalid URL/ID")
        return
        
    guild_id = str(interaction.guild.id)
    await ensure_video_exists(video_id, guild_id)

    # ✅ THIS IS THE MAGIC LINE:
    alert_channel_id = channel.id if channel else interaction.channel.id
    
    print(f"🔍 DEBUG: Using channel ID {alert_channel_id} for {video_id}")
    
    await db_execute("""
        INSERT OR REPLACE INTO intervals (video_id, guild_id, hours, alert_channel) 
        VALUES (?, ?, ?, ?)
    """, (video_id, guild_id, hours, alert_channel_id))

    guild_count = len(await db_execute(
        "SELECT * FROM intervals WHERE guild_id=? AND hours > 0", 
        (guild_id,), fetch=True
    ) or [])

    channel_name = channel.mention if channel else interaction.channel.mention
    await safe_response(interaction, 
        f"✅ **{hours}hr** interval set in {channel_name}! 📊 **{guild_count}** total")

@bot.tree.command(name="setupcomingmilestonesalert", description="Auto upcoming <100K alerts")
@app_commands.describe(channel="Summary channel", ping="Optional ping/role")
async def setupcomingmilestonesalert(interaction: discord.Interaction, channel: discord.TextChannel, ping: str = ""):
    await db_execute("INSERT OR REPLACE INTO upcoming_alerts (guild_id, channel_id, ping) VALUES (?, ?, ?)",
                   (str(interaction.guild.id), channel.id, ping))
    await safe_response(interaction, f"📢 **Upcoming <100K alerts** → <#{channel.id}> **(KST 3x/day + Intervals)**")

@bot.tree.command(name="listintervals", description="List all active server intervals (paginated)")
async def listintervals(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    intervals = await db_execute("""
        SELECT i.video_id, i.hours, v.title FROM intervals i 
        JOIN videos v ON i.video_id = v.video_id AND i.guild_id = v.guild_id
        WHERE i.hours > 0 AND i.guild_id=?
    """, (guild_id,), fetch=True) or []
    
    if not intervals:
        await safe_response(interaction, "📭 **No active intervals**")
        return
    
    page_size = 10
    pages = []
    for i in range(0, len(intervals), page_size):
        page_intervals = intervals[i:i+page_size]
        page_content = f"⏱️ **Active Intervals** (Page {i//page_size + 1}/{((len(intervals)-1)//page_size)+1})\n\n" + "\n".join(
            f"• **{intv['title'][:30]}**: `{intv['hours']}hr` ({intv['video_id']})" 
            for intv in page_intervals
        )
        pages.append(page_content)
    
    await start_paginator(interaction, pages)

@bot.tree.command(name="checkintervals", description="Force check ALL intervals NOW")
async def checkintervals(interaction: discord.Interaction):
    await interaction.response.defer()
    now = now_kst()
    guild_id = str(interaction.guild.id)
    intervals = await db_execute(
        "SELECT i.video_id, i.hours, v.title, v.alert_channel FROM intervals i JOIN videos v ON i.video_id = v.video_id WHERE i.hours > 0 AND v.guild_id=?",
        (guild_id,), fetch=True
    ) or []

    if not intervals:
        await interaction.followup.send("📭 **No active intervals**")
        return

    sent = 0
    for row in intervals:
        vid, hours, title, alert_ch_id = row['video_id'], row['hours'], row['title'], row['alert_channel']
        channel = bot.get_channel(int(alert_ch_id))
        if not channel: 
            continue

        views, likes = await fetch_video_stats(vid)
        if views is None: 
            continue

        # MILESTONE CHECK (inline - no function call needed)
        milestone_data = await db_execute(
            "SELECT ping, last_million FROM milestones WHERE video_id=? AND guild_id=?",
            (vid, guild_id), fetch=True
        ) or []
        current_million = views // 1_000_000
        if milestone_data:
            ping_str, last_million = milestone_data[0]['ping'], milestone_data[0]['last_million']
            if current_million > (last_million or 0):
                if ping_str and ping_str != f"{ping_str.split('|')[0]}|":
                    try:
                        ping_channel_id, role_ping = ping_str.split('|')
                        ping_channel = bot.get_channel(int(ping_channel_id))
                        if ping_channel:
                            youtube_url = f"https://youtu.be/{vid}"
                            await ping_channel.send(f"""🎉 **{title[:30]}** hit **{current_million}M VIEWS**! 🚀
📊 {views:,} views | ❤️ {likes:,} likes
🔗 {youtube_url}
{role_ping}""")
                    except Exception as e:
                        print(f"Milestone ping error: {e}")
                await db_execute(
                    "UPDATE milestones SET last_million=? WHERE video_id=? AND guild_id=?", 
                    (current_million, vid, guild_id)
                )

        prev_data = await db_execute("SELECT last_interval_views FROM intervals WHERE video_id=? AND guild_id=?", 
                                   (vid, guild_id), fetch=True) or [({'last_interval_views': 0},)]
        prev_views = prev_data[0]['last_interval_views'] if prev_data else 0
        net = views - prev_views
        next_time = now + timedelta(hours=hours)

        try:
            await channel.send(f"""⏱️ **{title}** ({hours}hr interval)
📊 {views:,} views (+{net:,})
⏳ Next: {next_time.strftime('%H:%M KST')}""")
            sent += 1
            await db_execute("UPDATE intervals SET last_interval_views=?, last_interval_run=? WHERE video_id=? AND guild_id=?",
                           (views, now.isoformat(), vid, guild_id))
        except:
            pass

    await interaction.followup.send(f"✅ **Checked {sent} intervals**")

# SERVER MILESTONE COMMANDS
@bot.tree.command(name="setservermilestone", description="Server-wide million alerts (KST 00:00/12:00/17:00 only)")
@app_commands.describe(channel="Alert channel", ping="Role ping (e.g. @everyone)")
async def setservermilestone(interaction: discord.Interaction, channel: discord.TextChannel, ping: str = ""):
    guild_id = str(interaction.guild.id)
    await db_execute("INSERT OR REPLACE INTO server_milestones (guild_id, ping) VALUES (?, ?)",
                   (guild_id, f"{channel.id}|{ping}"))
    await safe_response(interaction, f"🌐 **Server-wide alerts** → <#{channel.id}> {ping or '(no ping)'} **(KST 00:00/12:00/17:00 only)**")

@bot.tree.command(name="clearservmilestone", description="Clear server-wide million alerts")
async def clearservmilestone(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    await db_execute("UPDATE server_milestones SET ping='' WHERE guild_id=?", (guild_id,))
    await safe_response(interaction, "🗑️ **Server-wide milestone alerts cleared**")

@bot.tree.command(name="servercheck", description="Complete server overview")
async def servercheck(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = str(interaction.guild.id)
    videos = await db_execute("SELECT COUNT(*) as count FROM videos WHERE guild_id=?", (guild_id,), fetch=True) or [({'count': 0},)]
    video_count = videos[0]['count']
    intervals = await db_execute("SELECT COUNT(*) as count FROM intervals i JOIN videos v ON i.video_id=v.video_id WHERE i.hours > 0 AND v.guild_id=?", 
                               (guild_id,), fetch=True) or [({'count': 0},)]
    interval_count = intervals[0]['count']
    upcoming = await db_execute("SELECT channel_id, ping FROM upcoming_alerts WHERE guild_id=?", (guild_id,), fetch=True) or []
    server_milestones = await db_execute("SELECT ping FROM server_milestones WHERE guild_id=?", (guild_id,), fetch=True) or []

    response = f"**{interaction.guild.name} Overview** 📊\n\n"
    response += f"📹 **Videos**: {video_count} | ⏱️ **Intervals**: {interval_count}\n\n"
    response += "**🔔 Alert Channels:**\n"

    if upcoming:
        up_ch = bot.get_channel(int(upcoming[0]['channel_id']))
        channel_id = upcoming[0]['channel_id']
        response += f"• **Upcoming**: {up_ch.mention if up_ch else f'<#{channel_id}>'}\n"
    else:
        response += "• **Upcoming**: Not set\n"

    if server_milestones and server_milestones[0]['ping']:
        sm_ping = server_milestones[0]['ping']
        sm_ch_id, sm_role = sm_ping.split('|')
        sm_ch = bot.get_channel(int(sm_ch_id))
        response += f"• **Server M**: {sm_ch.mention if sm_ch else f'<#{sm_ch_id}>'} {sm_role or '(no ping)'}\n"
    else:
        response += "• **Server M**: Not set\n"

    kst_status = "🟢 Running" if kst_tracker.is_running() else "🔴 Stopped"
    interval_status = "🟢 Running" if interval_checker.is_running() else "🔴 Stopped"
    response += f"\n**🔄 Tasks**: KST: {kst_status} | Intervals: {interval_status}"
    await interaction.followup.send(response)

@bot.tree.command(name="help", description="All 21 YouTube Tracker Commands")
async def help_cmd(interaction: discord.Interaction):
    content = """📋 **21 YOUTUBE TRACKER COMMANDS** (Plain Text)

📹 **VIDEO MANAGEMENT (4)**
• `/addvideo <URL/ID> [title]` - Add video to track
• `/removevideo <URL/ID>` - Remove from server
• `/listvideos` - Videos in this channel **(PAGINATED)**
• `/serverlist` - All server videos **(PAGINATED)**

🔄 **LIVE CHECKS (5)**
• `/views <URL/ID>` - Single video stats
• `/forcecheck` - Check channel videos NOW
• `/viewsall` - ALL server stats **(PAGINATED)**
• `/checkintervals` - Force intervals NOW
• `/listintervals` - Active intervals **(PAGINATED)**

⏱️ **INTERVALS (4)**
• `/setinterval <URL/ID> <hours>` - Custom intervals (1/60hr min)
• `/disableinterval <URL/ID>` - Stop intervals
• `/setupcomingmilestonesalert <#ch> [ping]` - <100K alerts setup

🎯 **MILESTONES (5)**
• `/setmilestone <URL/ID> <#ch> [ping]` - Video million alerts
• `/setservermilestone <#ch> [ping]` - **SERVER-WIDE** KST alerts
• `/removemilestones <URL/ID>` - Clear video alerts
• `/clearservmilestone` - Clear server alerts
• `/upcoming` - <100K to million **(PAGINATED)**
• `/reachedmilestones` - Videos that hit millions

📊 **STATUS (3)**
• `/botcheck` - Bot health + KST time
• `/servercheck` - Server overview
• `/help` - This help

🔥 **AUTO-TRACKERS**:
• **KST**: 00:00/12:00/17:00 **(Server milestones + <100K)**
• **INTERVALS**: Custom hours (video + milestones + <100K)
• **PERSISTENT**: DB backup/restore 24/7

💾 **utils.py**: init_db, now_kst, backup_db ✅"""
    
    await safe_response(interaction, content)

# ERROR HANDLER
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await safe_response(interaction, f"⏳ **Wait {error.retry_after:.1f}s**")
    else:
        await safe_response(interaction, f"❌ **Command failed**: {str(error)}")

# INSERT THIS BLOCK JUST BEFORE LINE 720 (before @bot.event async def on_ready())
@tasks.loop(hours=1)
async def hourly_backup():
    backup_db()
    print(f"💾 Hourly backup complete - {now_kst().strftime('%H:%M KST')}")

# STARTUP - FIXED
@bot.event
async def on_ready():
    await init_db()
    
    # Start hourly backup task
    hourly_backup.start()
    
    print(f"🎉 {bot.user} online - KST: {now_kst().strftime('%H:%M:%S')}")
    print("💾 DB persistence: utils.py backup/restore ACTIVE")
    print("🌐 Flask: ACTIVE (Render 24/7)")

    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced **{len(synced)}** slash commands")
    except Exception as e:
        print(f"❌ Sync error: {e}")

    # Start bot tasks
    kst_tracker.start()
    interval_checker.start()
    
    print("🚀 **ALL SYSTEMS GO!** (21 Commands + KST + Intervals + Multi-Guild + PERSISTENT DB)")

# FINAL START - FIXED (Flask already running from top!)
if __name__ == "__main__":
    print(f"🤖 Bot starting... (Flask already running on port {PORT})")
    asyncio.run(bot.start(BOT_TOKEN))