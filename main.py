import discord
from discord.ext import tasks, commands
from discord import app_commands
import os
import asyncio
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
from utils import *  # PERFECTLY COMPATIBLE

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))

if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

# Flask Keepalive
app = Flask(__name__)
@app.route("/")
def home():
    return {"status": "alive", "time": now_kst().isoformat()}

@app.route("/health")
def health():
    return {"db": "sqlite3", "status": "running"}

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False)

# Safe response handler (40060-proof)
async def safe_response(interaction, content, ephemeral=False):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except:
        try:
            await interaction.followup.send(content, ephemeral=ephemeral)
        except:
            print(f"Failed to respond: {content}")

# INTERVAL TRACKER
@tasks.loop(minutes=1)
async def tracking_loop():
    now = now_kst()
    intervals = await db_execute("SELECT video_id, hours, first_run_time FROM intervals WHERE hours > 0", fetch=True)
    
    for vid, hours, first_run_iso in intervals or []:
        try:
            first_run = datetime.fromisoformat(first_run_iso).replace(tzinfo=KST)
            minutes_elapsed = int((now - first_run).total_seconds() / 60)
            interval_mins = int(hours * 60)  # 15 for 0.25hr
            
            # Fire on exact multiples: 15, 30, 45, 60...
            if minutes_elapsed % interval_mins == 0 and minutes_elapsed > 0:
                video = await db_execute("SELECT title, channel_id FROM videos WHERE video_id=?", (vid,), fetch=True)
                if video:
                    title, ch_id = video[0]
                    channel = bot.get_channel(int(ch_id))
                    if channel:
                        views = await fetch_views(vid)
                        if views:
                            next_mins = ((minutes_elapsed // interval_mins) + 1) * interval_mins
                            next_time = first_run + timedelta(minutes=next_mins)
                            await channel.send(
                                f"⏱️ **{title}** ({hours}hr)\n"
                                f"📊 **{views:,} views**\n"
                                f"⏳ **Next**: {next_time.strftime('%H:%M KST')}"
                            )
        except:
            continue
          
# KST TRACKER (00:00, 12:00, 17:00)
@tasks.loop(minutes=1)
async def kst_tracker():
    try:
        now = now_kst()
        if now.hour not in TRACK_HOURS or now.minute != 0:
            return

        print(f"KST Check: {now.strftime('%H:%M KST')}")
        videos = await db_execute("SELECT * FROM videos", fetch=True)

        for video in videos or []:
            key, vid, title, guild_id, ch_id, alert_ch = video
            views = await fetch_views(vid)
            if views is None:
                continue

            kst_data = await db_execute("SELECT kst_last_views FROM intervals WHERE video_id=?", (vid,), fetch=True)
            kst_last = kst_data[0][0] if kst_data and kst_data[0][0] else 0
            kst_net = f"(+{views-kst_last:,})" if kst_last else ""

            channel = bot.get_channel(int(alert_ch))
            if channel:
                try:
                    await channel.send(f"📅 **{now.strftime('%Y-%m-%d %H:%M KST')}**\n👀 **{title}** — {views:,} views {kst_net}")
                except:
                    pass

            await db_execute("UPDATE intervals SET kst_last_views=?, kst_last_run=?, last_views=? WHERE video_id=?", 
                           (views, now.isoformat(), views, vid))
    except Exception as e:
        print(f"KST Tracker Error: {e}")

# 🔥 ALL 16 COMMANDS - CLEAN SYNTAX
@bot.tree.command(name="botcheck", description="Bot status")
async def botcheck(interaction: discord.Interaction):
    now = now_kst()
    vcount = len(await db_execute("SELECT * FROM videos", fetch=True))
    icount = len(await db_execute("SELECT * FROM intervals WHERE hours > 0", fetch=True))
    kst_status = "🟢" if kst_tracker.is_running() else "🔴"
    interval_status = "🟢" if tracking_loop.is_running() else "🔴"
    
    await safe_response(interaction, 
        f"✅ **KST**: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📊 **{vcount}** videos | **{icount}** intervals\n"
        f"🔄 KST: {kst_status} | Interval: {interval_status}\n"
        f"💾 DB: {DB_PATH}\n🌐 PORT: {PORT}")

@bot.tree.command(name="addvideo", description="Add video to track")
@app_commands.describe(video_id="YouTube video ID", title="Video title")
async def addvideo(interaction: discord.Interaction, video_id: str, title: str = ""):
    await ensure_video_exists(video_id, str(interaction.guild.id), title, interaction.channel.id, interaction.channel.id)
    await safe_response(interaction, f"✅ **{title or video_id}** → <#{interaction.channel.id}>")

@bot.tree.command(name="removevideo", description="Remove video")
@app_commands.describe(video_id="YouTube video ID")
async def removevideo(interaction: discord.Interaction, video_id: str):
    count = len(await db_execute("SELECT * FROM videos WHERE video_id=? AND guild_id=?", (video_id, str(interaction.guild.id)), fetch=True))
    await db_execute("DELETE FROM videos WHERE video_id=? AND guild_id=?", (video_id, str(interaction.guild.id)))
    if not await db_execute("SELECT 1 FROM videos WHERE video_id=?", (video_id,), fetch=True):
        await db_execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
        await db_execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
    await safe_response(interaction, f"🗑️ Removed {count} video(s)")

@bot.tree.command(name="listvideos", description="Channel videos")
async def listvideos(interaction: discord.Interaction):
    videos = await db_execute("SELECT title FROM videos WHERE channel_id=?", (interaction.channel.id,), fetch=True)
    if not videos:
        await safe_response(interaction, "📭 No videos in this channel")
    else:
        await safe_response(interaction, "📋 **Channel videos:**\n" + "\n".join(f"• {v[0]}" for v in videos))

@bot.tree.command(name="serverlist", description="Server videos")
async def serverlist(interaction: discord.Interaction):
    videos = await db_execute("SELECT title FROM videos WHERE guild_id=?", (str(interaction.guild.id),), fetch=True)
    if not videos:
        await safe_response(interaction, "📭 No server videos")
    else:
        await safe_response(interaction, "📋 **Server videos:**\n" + "\n".join(f"• {v[0]}" for v in videos))

@bot.tree.command(name="forcecheck", description="Force check now")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = await db_execute("SELECT title, video_id FROM videos WHERE channel_id=?", (interaction.channel.id,), fetch=True)
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
    videos = await db_execute("SELECT title, video_id FROM videos WHERE guild_id=?", (str(interaction.guild.id),), fetch=True)
    if not videos:
        await interaction.followup.send("⚠️ No videos in server")
        return
    for title, vid in videos:
        views = await fetch_views(vid)
        if views:
            await db_execute("UPDATE intervals SET last_views=?, kst_last_views=? WHERE video_id=?", (views, views, vid))
            await interaction.followup.send(f"📊 **{title}**: {views:,}")

@bot.tree.command(name="setmilestone", description="Set milestone alerts")
@app_commands.describe(video_id="Video ID", channel="Alert channel", ping="Optional ping")
async def setmilestone(interaction: discord.Interaction, video_id: str, channel: discord.TextChannel, ping: str = ""):
    await db_execute("INSERT OR REPLACE INTO milestones (video_id, ping) VALUES (?, ?)", (video_id, f"{channel.id}|{ping}"))
    await safe_response(interaction, f"💿 Milestone alerts → <#{channel.id}>")

@bot.tree.command(name="reachedmilestones", description="Show reached milestones")
async def reachedmilestones(interaction: discord.Interaction):
    await interaction.response.defer()
    data = await db_execute("SELECT v.title, m.last_million FROM milestones m JOIN videos v ON m.video_id=v.video_id WHERE v.guild_id=? AND m.last_million > 0", (str(interaction.guild.id),), fetch=True)
    if not data:
        await interaction.followup.send("📭 No milestones reached")
    else:
        await interaction.followup.send("💿 **Reached:**\n" + "\n".join(f"• **{t}**: {m}M" for t, m in data))

@bot.tree.command(name="removemilestones", description="Clear milestone alerts")
@app_commands.describe(video_id="Video ID")
async def removemilestones(interaction: discord.Interaction, video_id: str):
    await db_execute("UPDATE milestones SET ping='' WHERE video_id=?", (video_id,))
    await safe_response(interaction, "✅ Milestone alerts cleared")

@bot.tree.command(name="setinterval", description="Set interval")
async def setinterval(interaction: discord.Interaction, video_id: str, hours: float):
    if hours < 0.25: return await safe_response(interaction, "❌ Min 15min")
    await ensure_video_exists(video_id, str(interaction.guild.id))
    
    now = now_kst()
    await db_execute("UPDATE intervals SET hours=?, first_run_time=? WHERE video_id=?", 
                    (hours, now.isoformat(), video_id))
    
    await safe_response(interaction, f"⏱️ **{hours}hr** intervals started!")

@bot.tree.command(name="disableinterval", description="Stop intervals")
@app_commands.describe(video_id="Video ID")
async def disableinterval(interaction: discord.Interaction, video_id: str):
    await db_execute("UPDATE intervals SET hours=0 WHERE video_id=?", (video_id,))
    await safe_response(interaction, "⏹️ Interval updates stopped")

@bot.tree.command(name="setupcomingmilestonesalert", description="Upcoming alerts")
@app_commands.describe(channel="Summary channel", ping="Optional ping")
async def setupcomingmilestonesalert(interaction: discord.Interaction, channel: discord.TextChannel, ping: str = ""):
    await db_execute("INSERT OR REPLACE INTO upcoming_alerts (guild_id, channel_id, ping) VALUES (?, ?, ?)", 
                    (str(interaction.guild.id), channel.id, ping))
    await safe_response(interaction, f"📢 **<100K alerts** → <#{channel.id}>")

@bot.tree.command(name="upcoming", description="Upcoming milestones")
async def upcoming(interaction: discord.Interaction):
    await interaction.response.defer()
    videos = await db_execute("SELECT title, video_id FROM videos WHERE guild_id=?", (str(interaction.guild.id),), fetch=True)
    lines = []
    now = now_kst()
    for title, vid in videos or []:
        views = await fetch_views(vid)
        if views:
            next_m = ((views // 1_000_000) + 1) * 1_000_000
            diff = next_m - views
            if 0 < diff <= 100_000:
                eta = estimate_eta(views, next_m)
                lines.append(f"⏳ **{title}**: **{diff:,}** to {next_m:,} **(ETA: {eta})**")
    if lines:
        await interaction.followup.send(f"📊 **Upcoming (<100K)** ({now.strftime('%H:%M KST')}):\n" + "\n".join(lines))
    else:
        await interaction.followup.send("📭 No videos within 100K")

@bot.tree.command(name="servercheck", description="Server overview")
async def servercheck(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = str(interaction.guild.id)
    videos = await db_execute("SELECT title, video_id, channel_id, alert_channel FROM videos WHERE guild_id=?", (guild_id,), fetch=True)
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
        print(f"Slash Error: {error}")
        await safe_response(interaction, "❌ Command failed", True)

@bot.event
async def on_ready():
    await init_db()
    print(f"{bot.user} online - KST: {now_kst().strftime('%H:%M:%S')}")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Sync: {e}")

    kst_tracker.start()
    tracking_loop.start()
    Thread(target=run_flask, daemon=True).start()
    print("🎯 ALL SYSTEMS GO!")

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
