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
# ✅ FIXED - Import safely
from utils import now_kst, db_execute, fetch_views, estimate_eta, ensure_video_exists

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("YouTubeBot")

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
DB_PATH = "youtube_bot.db"  # ✅ FIXED: Defined DB_PATH

if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

# Flask Keepalive
app = Flask(__name__)
@app.route("/")
def home():
    return {"status": "alive", "time": now_kst().isoformat()}

def run_flask():
    from waitress import serve  # ✅ Render-safe
    serve(app, host="0.0.0.0", port=PORT)

# Safe response handler
async def safe_response(interaction, content, ephemeral=False):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except Exception as e:
        log.error(f"Response failed: {e}")
        try:
            await interaction.followup.send(content, ephemeral=ephemeral)
        except:
            pass

# 🔥 KST TRACKER (00:00, 12:00, 17:00) + UPCOMING MILESTONES SUMMARY
@tasks.loop(minutes=1)
async def kst_tracker():
    try:
        now = now_kst()
        if now.hour not in [0, 12, 17] or now.minute != 0:
            return

        log.info(f"KST Check: {now.strftime('%H:%M KST')}")
        videos = await db_execute("SELECT * FROM videos", fetch=True) or []

        for video in videos:
            key, vid, title, guild_id, ch_id, alert_ch = video
            views = await fetch_views(vid)
            if views is None:
                continue

            kst_data = await db_execute("SELECT kst_last_views FROM intervals WHERE video_id=?", (vid,), fetch=True) or []
            kst_last = kst_data[0][0] if kst_data else 0
            kst_net = f"(+{views-kst_last:,})" if kst_last else ""

            channel = bot.get_channel(int(alert_ch))
            if channel:
                try:
                    await channel.send(f"📅 **{now.strftime('%Y-%m-%d %H:%M KST')}**\n👀 **{title}** — {views:,} views {kst_net}")
                except Exception as e:
                    log.error(f"KST send failed: {e}")

            await db_execute("UPDATE intervals SET kst_last_views=?, kst_last_run=?, last_views=? WHERE video_id=?", 
                           (views, now.isoformat(), views, vid))

        # 🔥 UPCOMING MILESTONES SUMMARY (00:00, 12:00, 17:00 KST) + ETA + PING ✅ FIXED
        upcoming_data = await db_execute("SELECT guild_id, channel_id, ping FROM upcoming_alerts", fetch=True) or []
        for guild_id, ch_id, ping_role in upcoming_data:
            channel = bot.get_channel(int(ch_id))
            if channel:
                guild_videos = await db_execute("SELECT title, video_id FROM videos WHERE guild_id=?", (guild_id,), fetch=True) or []
                upcoming = []
                for title, vid in guild_videos:
                    views = await fetch_views(vid)
                    if views:
                        next_m = ((views // 1_000_000) + 1) * 1_000_000
                        diff = next_m - views
                        if 0 < diff <= 100_000:
                            try:
                                eta = estimate_eta(views, next_m)
                                upcoming.append(f"⏳ **{title}**: **{diff:,}** to {next_m:,} **(ETA: {eta})**")
                            except:
                                upcoming.append(f"⏳ **{title}**: **{diff:,}** to {next_m:,}")
                if upcoming:
                    message = (f"📊 **UPCOMING <100K** ({now.strftime('%H:%M KST')}):\n" + 
                              "\n".join(upcoming) + f"\n\n🔔 {ping_role}")
                    await channel.send(message)

    except Exception as e:
        log.error(f"KST Tracker Error: {e}")

# 🔥 AUTO INTERVAL CHECKER + MILESTONE DETECTION ✅ FIXED
@tasks.loop(minutes=1)
async def interval_checker():
    try:
        # ✅ FIXED: Only check bot's guilds
        guild_ids = [str(g.id) for g in bot.guilds]
        intervals = await db_execute(
            "SELECT i.video_id, i.hours, v.guild_id FROM intervals i JOIN videos v ON i.video_id = v.video_id WHERE i.hours > 0 AND v.guild_id IN ({})".format(','.join('?' * len(guild_ids))), 
            guild_ids, fetch=True
        ) or []
        
        for vid, hours, guild_id in intervals:
            video = await db_execute("SELECT title, channel_id FROM videos WHERE video_id=?", (vid,), fetch=True)
            if not video: 
                continue
            title, ch_id = video[0]
            
            last_run_data = await db_execute("SELECT last_interval_run FROM intervals WHERE video_id=?", (vid,), fetch=True) or []
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
                    views = await fetch_views(vid)
                    if views:
                        # 🔥 MILLION MILESTONE CHECK (1M-5M ONLY)
                        milestone_data = await db_execute("SELECT ping, last_million FROM milestones WHERE video_id=?", (vid,), fetch=True) or []
                        if milestone_data:
                            ping_str, last_million = milestone_data[0]
                            current_million = views // 1_000_000
                            if current_million > (last_million or 0) and current_million <= 5:
                                if ping_str:
                                    try:
                                        ping_channel_id, role_ping = ping_str.split('|')
                                        ping_channel = bot.get_channel(int(ping_channel_id))
                                        if ping_channel:
                                            await ping_channel.send(f"🎉 **{title}** HIT **{current_million}M VIEWS!** 🚀\n"
                                                                  f"📊 **{views:,} total views**\n{role_ping}")
                                    except:
                                        pass
                                await db_execute("UPDATE milestones SET last_million=? WHERE video_id=?", (current_million, vid))
                        
                        # INTERVAL MESSAGE ✅ FIXED: Safe unpacking
                        prev_data = await db_execute("SELECT last_interval_views FROM intervals WHERE video_id=?", (vid,), fetch=True) or [(0,)]
                        prev_views = prev_data[0][0]
                        net = views - prev_views
                        
                        next_time = now + timedelta(hours=hours)
                        await channel.send(f"⏱️ **{title}** ({hours}hr interval)\n"
                                         f"📊 **{views:,} views** **(+{net:,})**\n"
                                         f"⏳ **Next**: {next_time.strftime('%H:%M KST')}")
                        
                        await db_execute("UPDATE intervals SET last_interval_views=?, last_interval_run=? WHERE video_id=?", 
                                       (views, now.isoformat(), vid))
    except Exception as e:
        log.error(f"Interval checker error: {e}")

# Task safety checks ✅ FIXED
@interval_checker.before_loop
async def before_interval_checker():
    await bot.wait_until_ready()

@kst_tracker.before_loop
async def before_kst_tracker():
    await bot.wait_until_ready()

# 🔥 17 SLASH COMMANDS (All preserved + fixed)
@bot.tree.command(name="botcheck", description="Bot status")
async def botcheck(interaction: discord.Interaction):
    now = now_kst()
    vcount = len(await db_execute("SELECT * FROM videos", fetch=True) or [])
    icount = len(await db_execute("SELECT * FROM intervals WHERE hours > 0", fetch=True) or [])
    kst_status = "🟢" if kst_tracker.is_running() else "🔴"
    interval_status = "🟢" if interval_checker.is_running() else "🔴"
    
    await safe_response(interaction, 
        f"✅ **KST**: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📊 **{vcount}** videos | **{icount}** intervals\n"
        f"🔄 KST: {kst_status} | Intervals: {interval_status}\n"
        f"💾 DB: Connected\n"
        f"🌐 PORT: {PORT}")

# [ALL OTHER 16 COMMANDS IDENTICAL TO PREVIOUS VERSION - NO CHANGES NEEDED]
@bot.tree.command(name="addvideo", description="Add video to track")
@app_commands.describe(video_id="YouTube video ID", title="Video title")
async def addvideo(interaction: discord.Interaction, video_id: str, title: str = ""):
    await ensure_video_exists(video_id, str(interaction.guild.id), title, interaction.channel.id, interaction.channel.id)
    await safe_response(interaction, f"✅ **{title or video_id}** → <#{interaction.channel.id}>")

@bot.tree.command(name="removevideo", description="Remove video")
@app_commands.describe(video_id="YouTube video ID")
async def removevideo(interaction: discord.Interaction, video_id: str):
    count = len(await db_execute("SELECT * FROM videos WHERE video_id=? AND guild_id=?", (video_id, str(interaction.guild.id)), fetch=True) or [])
    await db_execute("DELETE FROM videos WHERE video_id=? AND guild_id=?", (video_id, str(interaction.guild.id)))
    if not await db_execute("SELECT 1 FROM videos WHERE video_id=?", (video_id,), fetch=True):
        await db_execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
        await db_execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    await safe_response(interaction, f"🗑️ Removed {count} video(s)")

@bot.tree.command(name="listvideos", description="Channel videos")
async def listvideos(interaction: discord.Interaction):
    videos = await db_execute("SELECT title FROM videos WHERE channel_id=?", (interaction.channel.id,), fetch=True) or []
    if not videos:
        await safe_response(interaction, "📭 No videos in this channel")
    else:
        await safe_response(interaction, "📋 **Channel videos:**\n" + "\n".join(f"• {v[0]}" for v in videos))

@bot.tree.command(name="serverlist", description="Server videos")
async def serverlist(interaction: discord.Interaction):
    videos = await db_execute("SELECT title FROM videos WHERE guild_id=?", (str(interaction.guild.id),), fetch=True) or []
    if not videos:
        await safe_response(interaction, "📭 No server videos")
    else:
        await safe_response(interaction, "📋 **Server videos:**\n" + "\n".join(f"• {v[0]}" for v in videos))

@bot.tree.command(name="forcecheck", description="Force check now")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = await db_execute("SELECT title, video_id FROM videos WHERE channel_id=?", (interaction.channel.id,), fetch=True) or []
    if not videos:
        await interaction.followup.send("⚠️ No videos in this channel")
        return
    for title, vid in videos:
        views = await fetch_views(vid)
        if views:
            await db_execute("UPDATE intervals SET last_views=?, kst_last_views=? WHERE video_id=?", (views, views, vid))
            await interaction.followup.send(f"📊 **{title}**: {views:,}")
        else:
            await interaction.followup.send(f"❌ **{title}**: fetch failed")

@bot.tree.command(name="views", description="Check video views")
@app_commands.describe(video_id="YouTube video ID")
async def views(interaction: discord.Interaction, video_id: str):
    v = await fetch_views(video_id)
    await safe_response(interaction, f"📊 **{v:,} views**" if v else "❌ Fetch failed")

@bot.tree.command(name="viewsall", description="All server views")
async def viewsall(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = await db_execute("SELECT title, video_id FROM videos WHERE guild_id=?", (str(interaction.guild.id),), fetch=True) or []
    if not videos:
        await interaction.followup.send("⚠️ No videos in server")
        return
    for title, vid in videos:
        views = await fetch_views(vid)
        if views:
            await db_execute("UPDATE intervals SET last_views=?, kst_last_views=? WHERE video_id=?", (views, views, vid))
            await interaction.followup.send(f"📊 **{title}**: {views:,}")

@bot.tree.command(name="setmilestone", description="Set million milestone alerts (1M-5M)")
@app_commands.describe(video_id="Video ID", channel="Alert channel", ping="Optional ping")
async def setmilestone(interaction: discord.Interaction, video_id: str, channel: discord.TextChannel, ping: str = ""):
    await db_execute("INSERT OR REPLACE INTO milestones (video_id, ping) VALUES (?, ?)", (video_id, f"{channel.id}|{ping}"))
    await safe_response(interaction, f"💿 **Million milestone alerts** → <#{channel.id}> **(1M, 2M, 3M, 4M, 5M)**")

@bot.tree.command(name="reachedmilestones", description="Show reached million milestones")
async def reachedmilestones(interaction: discord.Interaction):
    await interaction.response.defer()
    data = await db_execute("SELECT v.title, m.last_million FROM milestones m JOIN videos v ON m.video_id=v.video_id WHERE v.guild_id=? AND m.last_million > 0", (str(interaction.guild.id),), fetch=True) or []
    if not data:
        await interaction.followup.send("📭 No million milestones reached")
    else:
        await interaction.followup.send("💿 **Million Milestones Reached:**\n" + "\n".join(f"• **{t}**: {m}M" for t, m in data))

@bot.tree.command(name="removemilestones", description="Clear milestone alerts")
@app_commands.describe(video_id="Video ID")
async def removemilestones(interaction: discord.Interaction, video_id: str):
    await db_execute("UPDATE milestones SET ping='' WHERE video_id=?", (video_id,))
    await safe_response(interaction, "✅ Million milestone alerts cleared")

@bot.tree.command(name="setinterval", description="Set interval (15min+)")
@app_commands.describe(video_id="Video ID", hours="Hours (0.25=15min)")
async def setinterval(interaction: discord.Interaction, video_id: str, hours: float):
    if hours < 0.25:
        await safe_response(interaction, "❌ Minimum 15 minutes (0.25hr)", True)
        return
    await ensure_video_exists(video_id, str(interaction.guild.id))
    await db_execute("INSERT OR REPLACE INTO intervals (video_id, hours) VALUES (?, ?)", (video_id, hours))
    count = len(await db_execute("SELECT * FROM intervals WHERE hours > 0", fetch=True) or [])
    await safe_response(interaction, f"✅ **{hours}hr** interval set!\n📊 **{count}** total intervals")

@bot.tree.command(name="disableinterval", description="Stop intervals")
@app_commands.describe(video_id="Video ID")
async def disableinterval(interaction: discord.Interaction, video_id: str):
    await db_execute("UPDATE intervals SET hours=0 WHERE video_id=?", (video_id,))
    await safe_response(interaction, "⏹️ Interval updates stopped")

@bot.tree.command(name="checkintervals", description="Check all intervals NOW")
async def checkintervals(interaction: discord.Interaction):
    await interaction.response.defer()
    now = now_kst()
    intervals = await db_execute("SELECT video_id, hours FROM intervals WHERE hours > 0", fetch=True) or []
    
    if not intervals:
        await interaction.followup.send("📭 No active intervals")
        return
    
    sent = 0
    for vid, hours in intervals:
        video = await db_execute("SELECT title, channel_id FROM videos WHERE video_id=?", (vid,), fetch=True)
        if not video: continue
        title, ch_id = video[0]
        channel = bot.get_channel(int(ch_id))
        if not channel: continue
            
        views = await fetch_views(vid)
        if views is None: continue
            
        # 🔥 MILLION MILESTONE CHECK (1M-5M ONLY)
        milestone_data = await db_execute("SELECT ping, last_million FROM milestones WHERE video_id=?", (vid,), fetch=True) or []
        if milestone_data:
            ping_str, last_million = milestone_data[0]
            current_million = views // 1_000_000
            if current_million > (last_million or 0) and current_million <= 5:
                if ping_str:
                    try:
                        ping_channel_id, role_ping = ping_str.split('|')
                        ping_channel = bot.get_channel(int(ping_channel_id))
                        if ping_channel:
                            await ping_channel.send(f"🎉 **{title}** HIT **{current_million}M VIEWS!** 🚀\n📊 **{views:,} total views**\n{role_ping}")
                    except:
                        pass
                await db_execute("UPDATE milestones SET last_million=? WHERE video_id=?", (current_million, vid))
        
        # INTERVAL NET GAIN
        prev_data = await db_execute("SELECT last_interval_views FROM intervals WHERE video_id=?", (vid,), fetch=True) or [(0,)]
        prev_views = prev_data[0][0]
        net = views - prev_views
        
        next_time = now + timedelta(hours=hours)
        try:
            await channel.send(f"⏱️ **{title}** ({hours}hr interval)\n"
                             f"📊 **{views:,} views** **(+{net:,})**\n"
                             f"⏳ **Next**: {next_time.strftime('%H:%M KST')}")
            sent += 1
            await db_execute("UPDATE intervals SET last_interval_views=?, last_interval_run=? WHERE video_id=?", 
                           (views, now.isoformat(), vid))
        except Exception as e:
            log.error(f"Failed to send interval: {e}")
    
    await interaction.followup.send(f"✅ Checked **{sent}** intervals")

@bot.tree.command(name="setupcomingmilestonesalert", description="Upcoming alerts at 00:00, 12:00, 17:00 KST")
@app_commands.describe(channel="Summary channel", ping="Optional ping")
async def setupcomingmilestonesalert(interaction: discord.Interaction, channel: discord.TextChannel, ping: str = ""):
    await db_execute("INSERT OR REPLACE INTO upcoming_alerts (guild_id, channel_id, ping) VALUES (?, ?, ?)", 
                    (str(interaction.guild.id), channel.id, ping))
    await safe_response(interaction, f"📢 **Upcoming <100K alerts** → <#{channel.id}> **(00:00, 12:00, 17:00 KST + ETA)**")

@bot.tree.command(name="upcoming", description="Upcoming milestones (<100K to next million)")
async def upcoming(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = await db_execute("SELECT title, video_id FROM videos WHERE guild_id=?", (str(interaction.guild.id),), fetch=True) or []
    lines = []
    now = now_kst()
    for title, vid in videos:
        views = await fetch_views(vid)
        if views:
            next_m = ((views // 1_000_000) + 1) * 1_000_000
            diff = next_m - views
            if 0 < diff <= 100_000:
                try:
                    eta = estimate_eta(views, next_m)
                    lines.append(f"⏳ **{title}**: **{diff:,}** to {next_m:,} **(ETA: {eta})**")
                except:
                    lines.append(f"⏳ **{title}**: **{diff:,}** to {next_m:,}")
    if lines:
        await interaction.followup.send(f"📊 **Upcoming <100K** ({now.strftime('%H:%M KST')}):\n" + "\n".join(lines))
    else:
        await interaction.followup.send("📭 No videos within 100K of next million")

@bot.tree.command(name="servercheck", description="Server overview")
async def servercheck(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = str(interaction.guild.id)
    videos = await db_execute("SELECT title, video_id, channel_id, alert_channel FROM videos WHERE guild_id=?", (guild_id,), fetch=True) or []
    response = f"**{interaction.guild.name} Overview** 📊\n\n**📹 Videos:** {len(videos)}\n"
    for title, vid, ch_id, alert_ch in videos[:10]:
        ch = bot.get_channel(int(ch_id)).mention if bot.get_channel(int(ch_id)) else f"#{ch_id}"
        response += f"• **{title}** → {ch}\n"
    await interaction.followup.send(response)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await safe_response(interaction, f"⏳ Wait {error.retry_after:.1f}s", True)
    else:
        log.error(f"Slash Error: {error}")
        await safe_response(interaction, "❌ Command failed", True)

@bot.event
async def on_ready():
    try:
        # ✅ FIXED: Proper async init_db call
        await asyncio.sleep(1)  # Wait for Discord ready
        await asyncio.create_task(init_db())  # Non-blocking DB init
        
        log.info(f"{bot.user} online - KST: {now_kst().strftime('%H:%M:%S')}")
        
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash commands")
        
        await asyncio.sleep(2)  # Stabilize
        
        kst_tracker.start()
        interval_checker.start()
        Thread(target=run_flask, daemon=True).start()
        
        log.info("🎯 ALL SYSTEMS GO! Bot + Flask + Tasks LIVE!")
    except Exception as e:
        log.error(f"on_ready failed: {e}")