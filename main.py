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
from utils import *
import shutil
import json
import atexit

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
DB_PATH = "youtube_bot.db"
BACKUP_PATH = os.path.join(os.getcwd(), "backup.db")

# Restore from backup
if os.path.exists(BACKUP_PATH) and not os.path.exists(DB_PATH):
    shutil.copy(BACKUP_PATH, DB_PATH)
    print("✅ Restored DB from backup")

# Auto-backup on exit
def backup_db():
    if os.path.exists(DB_PATH):
        shutil.copy(DB_PATH, BACKUP_PATH)
        print("✅ DB backed up")

atexit.register(backup_db)

# Disable PyNaCl voice warning
discord.utils.setup_warn_nacl(False)

if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN")

intents = discord.Intents.default()
intents.voice_states = False
bot = commands.Bot(command_prefix='!', intents=intents)

# Flask Keepalive
app = Flask(__name__)
@app.route("/")
@app.route("/health")
def home():
    return {"status": "alive", "time": now_kst().isoformat()}

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

async def safe_response(interaction, content, ephemeral=False):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except Exception as e:
        print(f"Response error: {e}")

# KST TRACKER (00:00, 12:00, 17:00) + INTEGRATED UPCOMING ALERTS
@tasks.loop(minutes=1)
async def kst_tracker():
    try:
        now = now_kst()
        if now.hour not in [0, 12, 17] or now.minute != 0:
            return

        print(f"🕐 KST Tracker running at {now.strftime('%H:%M KST')}")
        videos = await db_execute("SELECT * FROM videos", fetch=True) or []
        guild_upcoming = {}

        for video in videos:
            key, vid, title, guild_id, ch_id, alert_ch = video
            views, likes = await fetch_video_stats(vid)
            if views is None:
                continue

            # KST STATS MESSAGE
            kst_data = await db_execute("SELECT kst_last_views FROM intervals WHERE video_id=? AND guild_id=?", 
                                      (vid, guild_id), fetch=True) or []
            kst_last = kst_data[0][0] if kst_data else 0
            kst_net = f"(+{views-kst_last:,})" if kst_last else ""

            channel = bot.get_channel(int(alert_ch))
            if channel:
                await channel.send(f"""📅 **{now.strftime('%Y-%m-%d %H:%M KST')}**
👀 {title} — {views:,} views {kst_net}""")

            # UPDATE VIEW HISTORY
            history = await db_execute("SELECT view_history FROM intervals WHERE video_id=? AND guild_id=?", 
                                     (vid, guild_id), fetch=True) or [['[]']]
            try:
                hist = json.loads(history[0][0])
                hist.append({"views": views, "time": now.isoformat()})
                hist = hist[-10:]
                await db_execute("UPDATE intervals SET kst_last_views=?, kst_last_run=?, last_views=?, view_history=? WHERE video_id=? AND guild_id=?",
                    (views, now.isoformat(), views, json.dumps(hist), vid, guild_id))
            except:
                await db_execute("UPDATE intervals SET kst_last_views=?, kst_last_run=?, last_views=? WHERE video_id=? AND guild_id=?",
                    (views, now.isoformat(), views, vid, guild_id))

            await check_milestones(vid, title, views, likes, guild_id)

            # UPCOMING <100K DETECTION
            next_m = ((views // 1_000_000) + 1) * 1_000_000
            diff = next_m - views
            if 0 < diff <= 100_000 and guild_id not in guild_upcoming:
    guild_upcoming[guild_id] = []
            if 0 < diff <= 100_000:
    # ... rest of code (try/except block)

                try:
                    growth_rate = await get_real_growth_rate(vid, guild_id)
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

        # SEND UPCOMING SUMMARY PER GUILD
        for guild_id, upcoming_list in guild_upcoming.items():
            upcoming_data = await db_execute("SELECT channel_id, ping FROM upcoming_alerts WHERE guild_id=?", 
                                           (guild_id,), fetch=True) or []
            if upcoming_data and upcoming_list:
                ch_id, ping_role = upcoming_data[0]
                channel = bot.get_channel(int(ch_id))
                if channel:
                    message = f"""📊 **UPCOMING <100K** ({now.strftime('%H:%M KST')}):
{chr(10).join(upcoming_list)}
🔔 {ping_role}"""
                    await channel.send(message)
                    print(f"✅ Sent upcoming alert to guild {guild_id}: {len(upcoming_list)} videos")

    except Exception as e:
        print(f"KST tracker error: {e}")

# INTERVAL CHECKER (1min loop, guild-scoped) + UPCOMING
@tasks.loop(minutes=1)
async def interval_checker():
    try:
        intervals = await db_execute(
            "SELECT i.video_id, i.hours, v.guild_id, v.title, v.channel_id FROM intervals i JOIN videos v ON i.video_id = v.video_id WHERE i.hours > 0",
            fetch=True
        ) or []

        for vid, hours, guild_id, title, ch_id in intervals:
            last_run_data = await db_execute("SELECT last_interval_run FROM intervals WHERE video_id=? AND guild_id=?", 
                                           (vid, guild_id), fetch=True) or []
            if not last_run_data or not last_run_data[0][0]:
                continue

            try:
                last_time = datetime.fromisoformat(last_run_data[0][0])
            except:
                continue

            now = now_kst()
            if (now - last_time) >= timedelta(hours=hours):
                channel = bot.get_channel(int(ch_id))
                if channel:
                    views, likes = await fetch_video_stats(vid)
                    if views:
                        await check_milestones(vid, title, views, likes, guild_id)

                        prev_data = await db_execute("SELECT last_interval_views FROM intervals WHERE video_id=? AND guild_id=?", 
                                                   (vid, guild_id), fetch=True) or [(0,)]
                        prev_views = prev_data[0][0]
                        net = views - prev_views
                        next_time = now + timedelta(hours=hours)

                        # UPDATE HISTORY
                        history = await db_execute("SELECT view_history FROM intervals WHERE video_id=? AND guild_id=?", 
                                                 (vid, guild_id), fetch=True) or [[json.dumps([])]]
                        try:
                            hist = json.loads(history[0][0])
                            hist.append({"views": views, "time": now.isoformat()})
                            hist = hist[-10:]
                            await db_execute("UPDATE intervals SET last_interval_views=?, last_interval_run=?, view_history=? WHERE video_id=? AND guild_id=?",
                                           (views, now.isoformat(), json.dumps(hist), vid, guild_id))
                        except:
                            await db_execute("UPDATE intervals SET last_interval_views=?, last_interval_run=? WHERE video_id=? AND guild_id=?",
                                           (views, now.isoformat(), vid, guild_id))

                        await channel.send(f"""⏱️ **{title}** ({hours}hr interval)
📊 {views:,} views (+{net:,})
⏳ Next: {next_time.strftime('%H:%M KST')}""")

                        # UPCOMING ALERT ON INTERVAL
                        next_m = ((views // 1_000_000) + 1) * 1_000_000
                        diff = next_m - views
                        if 0 < diff <= 100_000:
                            upcoming_data = await db_execute("SELECT channel_id, ping FROM upcoming_alerts WHERE guild_id=?", 
                                                           (guild_id,), fetch=True) or []
                            if upcoming_data:
                                up_ch_id, ping_role = upcoming_data[0]
                                up_channel = bot.get_channel(int(up_ch_id))
                                if up_channel:
                                    try:
                                        growth_rate = await get_real_growth_rate(vid, guild_id)
                                        hours_to_m = diff / max(growth_rate, 10)
                                        eta = (f"{int(hours_to_m*60)}min" if hours_to_m < 1 else 
                                               f"{int(hours_to_m)}h" if hours_to_m < 24 else 
                                               f"{int(hours_to_m/24)}d" if hours_to_m < 168 else 
                                               f"{int(hours_to_m/24/7)}w")
                                        await up_channel.send(f"""📊 **UPCOMING <100K** ({now.strftime('%H:%M KST')}):
⏳ **{title}**: **{diff:,}** to {next_m:,} **(ETA: {eta})**
🔔 {ping_role}""")
                                    except:
                                        await up_channel.send(f"""📊 **UPCOMING <100K** ({now.strftime('%H:%M KST')}):
⏳ **{title}**: **{diff:,}** to {next_m:,}
🔔 {ping_role}""")

    except Exception as e:
        print(f"Interval checker error: {e}")

# CENTRAL MILESTONE CHECKER
async def check_milestones(vid, title, views, likes, guild_id):
    milestone_data = await db_execute(
        "SELECT m.ping, m.last_million FROM milestones m WHERE m.video_id=? AND m.guild_id=?",
        (vid, guild_id), fetch=True
    ) or []

    current_million = views // 1_000_000
    if milestone_data:
        ping_str, last_million = milestone_data[0]
        if current_million > (last_million or 0):
            if ping_str:
                try:
                    ping_channel_id, role_ping = ping_str.split('|')
                    ping_channel = bot.get_channel(int(ping_channel_id))
                    if ping_channel:
                        await ping_channel.send(f"""🎉 **{title[:30]}** hit **{current_million}M VIEWS**! 🚀
📊 {views:,} views | ❤️ {likes:,} likes
🔗 {title}
{role_ping}""")
                except Exception as e:
                    print(f"Milestone ping error: {e}")
            await db_execute("UPDATE milestones SET last_million=? WHERE video_id=? AND guild_id=?", 
                           (current_million, vid, guild_id))

    # Server-wide milestones
    server_ping = await db_execute("SELECT ping FROM server_milestones WHERE guild_id=?", (guild_id,), fetch=True)
    if server_ping and server_ping[0][0]:
        s_ping_str = server_ping[0][0]
        try:
            s_ch_id, s_role = s_ping_str.split('|')
            s_channel = bot.get_channel(int(s_ch_id))
            if s_channel:
                await s_channel.send(f"""🎉 **{title[:30]}** hit **{current_million}M**! 🚀
📊 {views:,} views | ❤️ {likes:,} likes
🔗 {title}
{s_role}""")
        except Exception as e:
            print(f"Server milestone error: {e}")

# Task startup hooks
@interval_checker.before_loop
async def before_interval_checker():
    await bot.wait_until_ready()

@kst_tracker.before_loop
async def before_kst_tracker():
    await bot.wait_until_ready()

# COMMAND 1-5: Core video management
@bot.tree.command(name="botcheck", description="Bot status and health")
@app_commands.describe()
async def botcheck(interaction: discord.Interaction):
    now = now_kst()
    vcount = len(await db_execute("SELECT * FROM videos", fetch=True) or [])
    icount = len(await db_execute("SELECT * FROM intervals WHERE hours > 0", fetch=True) or [])
    kst_status = "🟢" if kst_tracker.is_running() else "🔴"
    interval_status = "🟢" if interval_checker.is_running() else "🔴"
    await safe_response(interaction, f"""✅ **KST**: {now.strftime('%Y-%m-%d %H:%M:%S')}
📊 **{vcount}** videos | **{icount}** intervals
🔄 KST: {kst_status} | Intervals: {interval_status}
💾 DB: Connected | 🌐 PORT: {PORT}""")

@bot.tree.command(name="addvideo", description="Add YouTube video to track")
@app_commands.describe(video_id="YouTube video ID", title="Video title (optional)")
async def addvideo(interaction: discord.Interaction, video_id: str, title: str = ""):
    await ensure_video_exists(video_id, str(interaction.guild.id), title, interaction.channel.id, interaction.channel.id)
    await safe_response(interaction, f"✅ **{title or video_id}** → <#{interaction.channel.id}>")

@bot.tree.command(name="removevideo", description="Remove video from tracking")
@app_commands.describe(video_id="YouTube video ID")
async def removevideo(interaction: discord.Interaction, video_id: str):
    count = len(await db_execute("SELECT * FROM videos WHERE video_id=? AND guild_id=?", 
                               (video_id, str(interaction.guild.id)), fetch=True) or [])
    await db_execute("DELETE FROM videos WHERE video_id=? AND guild_id=?", (video_id, str(interaction.guild.id)))
    if not await db_execute("SELECT 1 FROM videos WHERE video_id=?", (video_id,), fetch=True):
        await db_execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
        await db_execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    await safe_response(interaction, f"🗑️ Removed **{count}** video(s)")

@bot.tree.command(name="listvideos", description="Videos in current channel")
@app_commands.describe()
async def listvideos(interaction: discord.Interaction):
    videos = await db_execute("SELECT title FROM videos WHERE channel_id=?", (interaction.channel.id,), fetch=True) or []
    if not videos:
        await safe_response(interaction, "📭 No videos in this channel")
    else:
        await safe_response(interaction, f"""📋 **Channel videos**:
{chr(10).join(f"• {v[0]}" for v in videos)}""")

@bot.tree.command(name="serverlist", description="All server videos")
@app_commands.describe()
async def serverlist(interaction: discord.Interaction):
    videos = await db_execute("SELECT title FROM videos WHERE guild_id=?", (str(interaction.guild.id),), fetch=True) or []
    if not videos:
        await safe_response(interaction, "📭 No server videos")
    else:
        await safe_response(interaction, "📋 **Server videos**:\n" + "\n".join(f"• {v[0]}" for v in videos))

# COMMAND 6-10: Manual stats checking
@bot.tree.command(name="forcecheck", description="Force check all channel videos NOW")
@app_commands.describe()
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = await db_execute("SELECT title, video_id FROM videos WHERE channel_id=?", (interaction.channel.id,), fetch=True) or []
    if not videos:
        await interaction.followup.send("⚠️ No videos in this channel")
        return
    results = []
    guild_id = str(interaction.guild.id)
    for title, vid in videos:
        views, likes = await fetch_video_stats(vid)
        if views:
            await db_execute("UPDATE intervals SET last_views=?, kst_last_views=?, view_history=? WHERE video_id=? AND guild_id=?", 
                           (views, views, json.dumps([{"views": views, "time": now_kst().isoformat()}]), vid, guild_id))
            await check_milestones(vid, title, views, likes, guild_id)
            results.append(f"📊 **{title}**: {views:,}❤️{likes:,}")
        else:
            results.append(f"❌ **{title}**: fetch failed")
    content = "📊 **Force check results**:\n" + "\n".join(results[:10])
    await interaction.followup.send(content)

@bot.tree.command(name="views", description="Check single video stats")
@app_commands.describe(video_id="YouTube video ID")
async def views(interaction: discord.Interaction, video_id: str):
    views, likes = await fetch_video_stats(video_id)
    if views:
        await safe_response(interaction, f"📊 **{views:,}** views | ❤️ **{likes:,}** likes")
    else:
        await safe_response(interaction, "❌ Fetch failed")

@bot.tree.command(name="viewsall", description="Check ALL server video stats")
@app_commands.describe()
async def viewsall(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = await db_execute("SELECT title, video_id FROM videos WHERE guild_id=?", (str(interaction.guild.id),), fetch=True) or []
    if not videos:
        await interaction.followup.send("⚠️ No videos in server")
        return
    guild_id = str(interaction.guild.id)
    results = []
    for title, vid in videos:
        views, likes = await fetch_video_stats(vid)
        if views:
            await db_execute("UPDATE intervals SET last_views=?, kst_last_views=? WHERE video_id=? AND guild_id=?", 
                           (views, views, vid, guild_id))
            await check_milestones(vid, title, views, likes, guild_id)
            results.append(f"📊 **{title}**: {views:,}❤️{likes:,}")
    await interaction.followup.send("📊 **Server stats**:\n" + "\n".join(results[:20]))

@bot.tree.command(name="reachedmilestones", description="Videos that hit millions")
@app_commands.describe()
async def reachedmilestones(interaction: discord.Interaction):
    await interaction.response.defer()
    data = await db_execute(
        "SELECT v.title, m.last_million FROM milestones m JOIN videos v ON m.video_id=v.video_id WHERE v.guild_id=? AND m.last_million > 0",
        (str(interaction.guild.id),), fetch=True
    ) or []
    if not data:
        await interaction.followup.send("📭 No million milestones reached")
    else:
        await interaction.followup.send("💿 **Million Milestones Reached**:\n" + "\n".join(f"• **{t}**: {m}M" for t, m in data))

@bot.tree.command(name="upcoming", description="Upcoming milestones (<100K to next million)")
@app_commands.describe(ping="Optional ping/role")
async def upcoming(interaction: discord.Interaction, ping: str = ""):
    await interaction.response.defer()
    guild_id = str(interaction.guild.id)
    videos = await db_execute("SELECT title, video_id FROM videos WHERE guild_id=?", (guild_id,), fetch=True) or []
    lines = []
    now = now_kst()
    for title, vid in videos:
        views, _ = await fetch_video_stats(vid)
        if views:
            next_m = ((views // 1_000_000) + 1) * 1_000_000
            diff = next_m - views
            if 0 < diff <= 100_000:
                try:
                    growth_rate = await get_real_growth_rate(vid, guild_id)
                    hours = (next_m - views) / max(growth_rate, 10)
                    if hours < 1:
                        eta = f"{int(hours*60)}min"
                    elif hours < 24:
                        eta = f"{int(hours)}h"
                    elif hours < 168:
                        eta = f"{int(hours/24)}d"
                    else:
                        eta = f"{int(hours/24/7)}w"
                    lines.append(f"⏳ **{title}**: **{diff:,}** to {next_m:,} **(ETA: {eta})**")
                except:
                    lines.append(f"⏳ **{title}**: **{diff:,}** to {next_m:,}")
    if lines:
        msg = f"""📊 **UPCOMING <100K** ({now.strftime('%H:%M KST')}):
{chr(10).join(lines)}
🔔 {ping}"""
        await interaction.followup.send(msg)
    else:
        await interaction.followup.send("📭 No videos within 100K of next million")

# COMMAND 11-19: Milestone & Interval Management
@bot.tree.command(name="setmilestone", description="Video million alerts")
@app_commands.describe(video_id="Video ID", channel="Alert channel", ping="Optional ping/role")
async def setmilestone(interaction: discord.Interaction, video_id: str, channel: discord.TextChannel, ping: str = ""):
    await ensure_video_exists(video_id, str(interaction.guild.id))
    await db_execute("INSERT OR REPLACE INTO milestones (video_id, guild_id, ping) VALUES (?, ?, ?)",
                   (video_id, str(interaction.guild.id), f"{channel.id}|{ping}"))
    await safe_response(interaction, f"💿 **Million alerts** → <#{channel.id}> **(every 1M+)** {ping or ''}")

@bot.tree.command(name="setservermilestones", description="Server-wide million alerts")
@app_commands.describe(channel="Alert channel", ping="Optional ping/role")
async def setservermilestones(interaction: discord.Interaction, channel: discord.TextChannel, ping: str = ""):
    ping_str = f"{channel.id}|{ping}"
    await db_execute("INSERT OR REPLACE INTO server_milestones (guild_id, ping) VALUES (?, ?)",
                   (str(interaction.guild.id), ping_str))
    await safe_response(interaction, f"💿 **Server milestones** → <#{channel.id}> {ping or ''}")

@bot.tree.command(name="removemilestones", description="Clear video milestone alerts")
@app_commands.describe(video_id="Video ID")
async def removemilestones(interaction: discord.Interaction, video_id: str):
    await db_execute("UPDATE milestones SET ping='' WHERE video_id=? AND guild_id=?", 
                   (video_id, str(interaction.guild.id)))
    await safe_response(interaction, "✅ **Video milestone alerts cleared**")

@bot.tree.command(name="clearservermilestones", description="Clear server milestones")
@app_commands.describe()
async def clearservermilestones(interaction: discord.Interaction):
    await db_execute("DELETE FROM server_milestones WHERE guild_id=?", (str(interaction.guild.id),))
    await safe_response(interaction, "✅ **Server milestones cleared**")

@bot.tree.command(name="setinterval", description="Set custom interval checks")
@app_commands.describe(video_id="Video ID", hours="Hours between checks (1/60=1min minimum)")
async def setinterval(interaction: discord.Interaction, video_id: str, hours: float):
    if hours < 1/60:  # CHANGED: Now 1 minute minimum
        await safe_response(interaction, "❌ **Minimum 1 minute (1/60 hr)**", ephemeral=True)
        return
    await ensure_video_exists(video_id, str(interaction.guild.id))
    await db_execute("INSERT OR REPLACE INTO intervals (video_id, guild_id, hours) VALUES (?, ?, ?)",
                   (video_id, str(interaction.guild.id), hours))
    count = len(await db_execute("SELECT * FROM intervals WHERE hours > 0", fetch=True) or [])
    await safe_response(interaction, f"✅ **{hours}hr** interval set!\n📊 **{count}** total intervals")

@bot.tree.command(name="disableinterval", description="Stop interval checks")
@app_commands.describe(video_id="Video ID")
async def disableinterval(interaction: discord.Interaction, video_id: str):
    await db_execute("UPDATE intervals SET hours=0 WHERE video_id=? AND guild_id=?", 
                   (video_id, str(interaction.guild.id)))
    await safe_response(interaction, "⏹️ **Interval updates stopped**")

@bot.tree.command(name="checkintervals", description="Force check ALL intervals NOW")
@app_commands.describe()
async def checkintervals(interaction: discord.Interaction):
    await interaction.response.defer()
    now = now_kst()
    guild_id = str(interaction.guild.id)
    intervals = await db_execute(
        "SELECT i.video_id, i.hours, v.title, v.channel_id FROM intervals i JOIN videos v ON i.video_id = v.video_id WHERE i.hours > 0 AND v.guild_id=?",
        (guild_id,), fetch=True
    ) or []

    if not intervals:
        await interaction.followup.send("📭 **No active intervals**")
        return

    sent = 0
    for vid, hours, title, ch_id in intervals:
        channel = bot.get_channel(int(ch_id))
        if not channel: continue

        views, likes = await fetch_video_stats(vid)
        if views is None: continue

        await check_milestones(vid, title, views, likes, guild_id)

        prev_data = await db_execute("SELECT last_interval_views FROM intervals WHERE video_id=? AND guild_id=?", 
                                   (vid, guild_id), fetch=True) or [(0,)]
        prev_views = prev_data[0][0]
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

@bot.tree.command(name="setupcomingmilestonesalert", description="Auto upcoming <100K alerts (KST+Interval)")
@app_commands.describe(channel="Summary channel", ping="Optional ping/role")
async def setupcomingmilestonesalert(interaction: discord.Interaction, channel: discord.TextChannel, ping: str = ""):
    await db_execute("INSERT OR REPLACE INTO upcoming_alerts (guild_id, channel_id, ping) VALUES (?, ?, ?)",
                   (str(interaction.guild.id), channel.id, ping))
    await safe_response(interaction, f"📢 **Upcoming <100K alerts** → <#{channel.id}> **(KST 3x/day + Intervals)**")

# BONUS: Server overview dashboard
@bot.tree.command(name="servercheck", description="Complete server overview")
@app_commands.describe()
async def servercheck(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = str(interaction.guild.id)
    videos = await db_execute("SELECT COUNT(*) FROM videos WHERE guild_id=?", (guild_id,), fetch=True) or [(0,)]
    video_count = videos[0][0]
    intervals = await db_execute("SELECT COUNT(*) FROM intervals i JOIN videos v ON i.video_id=v.video_id WHERE i.hours > 0 AND v.guild_id=?", 
                               (guild_id,), fetch=True) or [(0,)]
    interval_count = intervals[0][0]
    upcoming = await db_execute("SELECT channel_id, ping FROM upcoming_alerts WHERE guild_id=?", (guild_id,), fetch=True) or []
    server_milestones = await db_execute("SELECT ping FROM server_milestones WHERE guild_id=?", (guild_id,), fetch=True) or []

    kst_runs = await db_execute("SELECT DISTINCT kst_last_run FROM intervals i JOIN videos v ON i.video_id=v.video_id WHERE v.guild_id=? ORDER BY kst_last_run DESC LIMIT 3", 
                              (guild_id,), fetch=True) or []
    kst_status = []
    now = now_kst()
    for run_time in kst_runs:
        if run_time[0]:
            try:
                last_kst = datetime.fromisoformat(run_time[0])
                hours_ago = (now - last_kst).total_seconds() / 3600
                kst_status.append("✅ Recent" if hours_ago < 24 else "❌ >24h")
            except:
                kst_status.append("❌ Invalid")

    response = f"**{interaction.guild.name} Overview** 📊\n\n"
    response += f"📹 **Videos**: {video_count} | ⏱️ **Intervals**: {interval_count}\n\n"
    response += "**🔔 Alert Channels:**\n"
    if upcoming:
        up_ch = bot.get_channel(int(upcoming[0][0]))
        response += f"• **Upcoming**: {up_ch.mention if up_ch else f'<#{upcoming[0][0]}>'}\n"
    else:
        response += "• **Upcoming**: Not set\n"
    if server_milestones and server_milestones[0][0]:
        sm_ping = server_milestones[0][0]
        sm_ch_id, _ = sm_ping.split('|')
        sm_ch = bot.get_channel(int(sm_ch_id))
        response += f"• **Server M**: {sm_ch.mention if sm_ch else f'<#{sm_ch_id}>'}\n"
    else:
        response += "• **Server M**: Not set\n"
    response += f"\n**📅 KST**: {' | '.join(kst_status[:3]) or 'No data'}"
    await interaction.followup.send(response)

# ERROR HANDLER
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await safe_response(interaction, f"⏳ **Wait {error.retry_after:.1f}s**", ephemeral=True)
    else:
        await safe_response(interaction, "❌ **Command failed**", ephemeral=True)

# BOT STARTUP
@bot.event
async def on_ready():
    await init_db()
    print(f"🎉 {bot.user} online - KST: {now_kst().strftime('%H:%M:%S')}")

    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced **{len(synced)}** slash commands")
    except Exception as e:
        print(f"❌ Sync error: {e}")

    await asyncio.sleep(2)
    kst_tracker.start()
    interval_checker.start()
    Thread(target=run_flask, daemon=True).start()
    print("🚀 **ALL SYSTEMS GO!** (KST+Upcoming+Intervals+Real ETA+Likes+19 Commands)")

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    print(f"🌐 Flask started on port {PORT}")
    asyncio.run(bot.start(BOT_TOKEN))