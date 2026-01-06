import discord
from discord.ext import tasks, commands
from discord import app_commands
import aiohttp
import os
import aiosqlite
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
DB_PATH = os.getenv("DB_PATH", "yt_data.db")  # Local-friendly default

if not BOT_TOKEN or not YOUTUBE_API_KEY:
    raise ValueError("❌ Missing BOT_TOKEN or YOUTUBE_API_KEY")

# ========================================
# TIME (KST) - 100% Host-Independent
# ========================================
KST = pytz.timezone("Asia/Seoul")

def now_kst():
    return datetime.now(KST)

TRACK_HOURS = [0, 12, 17]  # 12AM, 12PM, 5PM KST

# ========================================
# ASYNC SQLITE3 (Thread-Safe) - FIXED
# ========================================
db_lock = asyncio.Lock()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS videos (
            key TEXT PRIMARY KEY, video_id TEXT, title TEXT, guild_id TEXT,
            channel_id TEXT, alert_channel TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS intervals (
            video_id TEXT PRIMARY KEY, hours REAL DEFAULT 0, next_run TEXT,
            last_views INTEGER DEFAULT 0, last_interval_views INTEGER DEFAULT 0,
            last_interval_run TEXT, kst_last_views INTEGER DEFAULT 0, kst_last_run TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS milestones (
            video_id TEXT PRIMARY KEY, last_million INTEGER DEFAULT 0, ping TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS upcoming_alerts (
            guild_id TEXT PRIMARY KEY, channel_id TEXT, ping TEXT
        )''')
        await db.commit()
    print(f"✅ SQLite3 initialized: {DB_PATH}")

async def db_execute(query, params=(), fetch=False):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                if fetch:
                    async with db.execute(query, params) as cursor:
                        return await cursor.fetchall()
                else:
                    await db.execute(query, params)
                    await db.commit()
                    return True
            except Exception as e:
                print(f"❌ DB Error: {e}")
                return False

# ========================================
# YOUTUBE API (Rate Limit Safe)
# ========================================
youtube_semaphore = asyncio.Semaphore(5)

async def fetch_views(video_id):
    async with youtube_semaphore:
        url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={video_id}&key={YOUTUBE_API_KEY}"
        for attempt in range(3):
            try:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as r:
                        if r.status != 200:
                            await asyncio.sleep(1)
                            continue
                        data = await r.json()
                        if not data.get("items"):
                            return None
                        return int(data["items"][0]["statistics"]["viewCount"])
            except:
                await asyncio.sleep(1 + attempt * 0.5)
        return None

def estimate_eta(current_views, target_views):
    remaining = target_views - current_views
    if remaining <= 0: return "NOW!"
    hours = max(1, remaining / 1000)
    if hours < 24: return f"{int(hours)}hr"
    return f"{int(hours/24)}d"

# ========================================
# FLASK (Render Keepalive)
# ========================================
app = Flask(__name__)

@app.route("/")
def home():
    return {"status": "alive", "time": now_kst().isoformat()}

@app.route("/health")
def health():
    return {"db": "sqlite3", "status": "running"}

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False)

# ========================================
# DISCORD BOT
# ========================================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

async def ensure_video_exists(video_id, guild_id, title="", channel_id=0, alert_channel=0):
    key = f"{video_id}_{guild_id}"
    exists = await db_execute("SELECT 1 FROM videos WHERE key=?", (key,), True)
    if not exists:
        await db_execute('''INSERT INTO videos (key, video_id, title, guild_id, channel_id, alert_channel)
                          VALUES (?, ?, ?, ?, ?, ?)''',
                        (key, video_id, title or video_id, guild_id, channel_id, alert_channel))
        await db_execute('''INSERT OR IGNORE INTO intervals (video_id) VALUES (?)''', (video_id,))
        await db_execute('''INSERT OR IGNORE INTO milestones (video_id) VALUES (?)''', (video_id,))

# ========================================
# KST TRACKER (Net from LAST KST)
# ========================================
@tasks.loop(minutes=1)
async def kst_tracker():
    try:
        now = now_kst()
        if now.hour not in TRACK_HOURS or now.minute != 0: return
        
        print(f"🔄 KST Check: {now.strftime('%H:%M')}")
        
        # 1. Regular video updates
        videos = await db_execute("SELECT * FROM videos")
        for video in videos or []:
            key, vid, title, guild_id, ch_id, alert_ch = video
            views = await fetch_views(vid)
            if views is None: continue
            
            kst_data = await db_execute("SELECT kst_last_views FROM intervals WHERE video_id=?", (vid,), True)
            kst_last = kst_data[0][0] if kst_data else 0
            kst_net = f"(+{views-kst_last:,})" if kst_last else ""
            
            channel = bot.get_channel(int(alert_ch))
            if channel:
                try:
                    await channel.send(f"""📅 **{now.strftime('%Y-%m-%d %H:%M KST')}**
👀 **{title}** — {views:,} views **{kst_net}**""")
                except: pass
            
            await db_execute("UPDATE intervals SET kst_last_views=?, kst_last_run=?, last_views=? WHERE video_id=?",
                           (views, now.isoformat(), views, vid))
            
            # Milestones
            milestone = await db_execute("SELECT ping, last_million FROM milestones WHERE video_id=?", (vid,), True)
            if milestone:
                ping_data, last_mil = milestone[0]
                million = views // 1_000_000
                if million > last_mil and ping_data:
                    try:
                        ch_id, msg = ping_data.split("|", 1)
                        mil_ch = bot.get_channel(int(ch_id))
                        if mil_ch:
                            await mil_ch.send(f"📼 **{title}** crossed **{million}M views**!
{msg}")
                        await db_execute("UPDATE milestones SET last_million=? WHERE video_id=?", (million, vid))
                    except: pass
        
        # 2. Upcoming milestones (<100K + ETA)
        alerts = await db_execute("SELECT * FROM upcoming_alerts")
        for alert in alerts or []:
            guild_id, ch_id, ping = alert
            channel = bot.get_channel(int(ch_id))
            if not channel: continue
            
            upcoming = []
            guild_videos = await db_execute("SELECT title, video_id FROM videos WHERE guild_id=?", (guild_id,), True)
            for title, vid in guild_videos or []:
                views = await fetch_views(vid)
                if views:
                    next_m = ((views // 1_000_000) + 1) * 1_000_000
                    diff = next_m - views
                    if 0 < diff <= 100_000:
                        eta = estimate_eta(views, next_m)
                        upcoming.append(f"⏳ **{title}**: **{diff:,}** to {next_m:,} **(ETA: {eta})**")
            
            if upcoming:
                try:
                    await channel.send(f"""📊 **Upcoming Milestones** ({now.strftime('%H:%M KST')}):
{chr(10)\n.join(upcoming)}
{ping}""")
                except: pass
                
    except Exception as e:
        print(f"❌ KST Tracker Error: {e}")

# ========================================
# INTERVAL TRACKER - FIXED
# ========================================
@tasks.loop(minutes=5)
async def tracking_loop():
    try:
        now = now_kst()
        intervals = await db_execute("SELECT video_id, hours, last_interval_views, last_interval_run FROM intervals WHERE hours > 0", fetch=True)
        
        for vid, hours, last_interval_views, last_interval_run in intervals or []:
            if last_interval_run:
                try:
                    last_time = datetime.fromisoformat(last_interval_run).replace(tzinfo=KST)
                    if (now - last_time).total_seconds() < hours * 3600 * 0.9: continue
                except: pass
            
            video = await db_execute("SELECT title, channel_id FROM videos WHERE video_id=?", (vid,), True)
            if not video: continue
            title, ch_id = video[0]
            channel = bot.get_channel(int(ch_id))
            if not channel: continue
            
            views = await fetch_views(vid)
            if views is None: continue
            
            net = views - (last_interval_views or 0)
            next_time = now + timedelta(hours=hours)
            
            try:
                await channel.send(f"""⏱️ **{title}** Interval
📊 {views:,} **(+{net:,})**
⏳ Next: {next_time.strftime('%H:%M KST')}""")
            except: pass
            
            await db_execute("UPDATE intervals SET next_run=?, last_views=?, last_interval_views=?, last_interval_run=? WHERE video_id=?",
                           (next_time.isoformat(), views, views, now.isoformat(), vid))
                           
    except Exception as e:
        print(f"❌ Interval Error: {e}")

# ========================================
# ALL 16 COMMANDS (FULLY IMPLEMENTED & FIXED)
# ========================================
@bot.tree.command(name="addvideo", description="Add video to track")
@app_commands.describe(video_id="YouTube video ID", title="Video title")
async def addvideo(interaction: discord.Interaction, video_id: str, title: str):
    try:
        await ensure_video_exists(video_id, str(interaction.guild.id), title, interaction.channel.id, interaction.channel.id)
        await interaction.response.send_message(f"✅ **{title}** → <#{interaction.channel.id}>")
    except Exception as e:
        await interaction.response.send_message("❌ Failed to add video", ephemeral=True)

@bot.tree.command(name="removevideo", description="Remove video from tracking")
@app_commands.describe(video_id="YouTube video ID")
async def removevideo(interaction: discord.Interaction, video_id: str):
    try:
        count = len(await db_execute("SELECT * FROM videos WHERE video_id=? AND guild_id=?", (video_id, str(interaction.guild.id)), fetch=True))
        await db_execute("DELETE FROM videos WHERE video_id=? AND guild_id=?", (video_id, str(interaction.guild.id)))
        if not await db_execute("SELECT 1 FROM videos WHERE video_id=?", (video_id,), True):
            await db_execute("DELETE FROM intervals WHERE video_id=?", (video_id,))
            await db_execute("DELETE FROM milestones WHERE video_id=?", (video_id,))
        await interaction.response.send_message(f"🗑️ Removed {count} video(s)")
    except:
        await interaction.response.send_message("❌ Failed to remove", ephemeral=True)

@bot.tree.command(name="listvideos", description="Videos tracked in this channel")
async def listvideos(interaction: discord.Interaction):
    try:
        videos = await db_execute("SELECT title FROM videos WHERE channel_id=?", (interaction.channel.id,), True)
        if not videos:
            await interaction.response.send_message("📭 No videos in this channel")
        else:
            await interaction.response.send_message("📋 **Channel videos:**
" + "
".join(f"• {v[0]}" for v in videos))
    except:
        await interaction.response.send_message("❌ Error fetching list", ephemeral=True)

@bot.tree.command(name="serverlist", description="All server videos")
async def serverlist(interaction: discord.Interaction):
    try:
        videos = await db_execute("SELECT title FROM videos WHERE guild_id=?", (str(interaction.guild.id),), True)
        if not videos:
            await interaction.response.send_message("📭 No server videos")
        else:
            await interaction.response.send_message("📋 **Server videos:**
" + "
".join(f"• {v[0]}" for v in videos))
    except:
        await interaction.response.send_message("❌ Error fetching server list", ephemeral=True)


@bot.tree.command(name="forcecheck", description="Force check channel videos NOW")
async def forcecheck(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        videos = await db_execute("SELECT title, video_id FROM videos WHERE channel_id=?", (interaction.channel.id,), True)
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
    except:
        await interaction.followup.send("❌ Force check failed")

@bot.tree.command(name="views", description="Check single video views")
@app_commands.describe(video_id="YouTube video ID")
async def views(interaction: discord.Interaction, video_id: str):
    try:
        v = await fetch_views(video_id)
        await interaction.response.send_message(f"📊 **{v:,} views**" if v else "❌ Fetch failed")
    except:
        await interaction.response.send_message("❌ Failed to fetch views", ephemeral=True)

@bot.tree.command(name="viewsall", description="Check all server video views")
async def viewsall(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        videos = await db_execute("SELECT title, video_id FROM videos WHERE guild_id=?", (str(interaction.guild.id),), True)
        if not videos:
            await interaction.followup.send("⚠️ No videos in server")
            return
        for title, vid in videos:
            views = await fetch_views(vid)
            if views:
                await db_execute("UPDATE intervals SET last_views=?, kst_last_views=? WHERE video_id=?", (views, views, vid))
                await interaction.followup.send(f"📊 **{title}**: {views:,}")
    except:
        await interaction.followup.send("❌ Views check failed")

@bot.tree.command(name="setmilestone", description="Set milestone alerts")
@app_commands.describe(video_id="Video ID", channel="Alert channel", ping="Optional ping message")
async def setmilestone(interaction: discord.Interaction, video_id: str, channel: discord.TextChannel, ping: str = ""):
    try:
        await db_execute("INSERT OR REPLACE INTO milestones (video_id, ping) VALUES (?, ?)",
                        (video_id, f"{channel.id}|{ping}"))
        await interaction.response.send_message(f"📼 Milestone alerts set → <#{channel.id}>")
    except:
        await interaction.response.send_message("❌ Failed to set milestone", ephemeral=True)

@bot.tree.command(name="reachedmilestones", description="Show reached milestones")
async def reachedmilestones(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        data = await db_execute("SELECT v.title, m.last_million FROM milestones m JOIN videos v ON m.video_id=v.video_id WHERE v.guild_id=? AND m.last_million > 0",
                              (str(interaction.guild.id),), True)
        if not data:
            await interaction.followup.send("📭 No milestones reached yet")
        else:
            await interaction.followup.send("📼 **Reached milestones:**
" + "
".join(f"• **{t}**: {m}M" for t, m in data))
    except:
        await interaction.followup.send("❌ Failed to fetch milestones")

@bot.tree.command(name="removemilestones", description="Clear milestone alerts")
@app_commands.describe(video_id="Video ID")
async def removemilestones(interaction: discord.Interaction, video_id: str):
    try:
        await db_execute("UPDATE milestones SET ping='' WHERE video_id=?", (video_id,))
        await interaction.response.send_message("❌ Milestone alerts cleared")
    except:
        await interaction.response.send_message("❌ Failed to clear alerts", ephemeral=True)

@bot.tree.command(name="setinterval", description="Set custom interval updates")
@app_commands.describe(video_id="Video ID", hours="Hours between checks (1-24)")
async def setinterval(interaction: discord.Interaction, video_id: str, hours: float):
    try:
        if hours < 1 or hours > 24:
            return await interaction.response.send_message("❌ Hours must be 1-24", ephemeral=True)
        await ensure_video_exists(video_id, str(interaction.guild.id))
        now = now_kst()
        next_time = now + timedelta(hours=hours)
        await db_execute("UPDATE intervals SET hours=?, next_run=? WHERE video_id=?",
                        (hours, next_time.isoformat(), video_id))
        await interaction.response.send_message(f"⏱️ **{hours}hr** intervals → **{next_time.strftime('%H:%M KST')}**")
    except:
        await interaction.response.send_message("❌ Failed to set interval", ephemeral=True)

@bot.tree.command(name="disableinterval", description="Stop interval updates")
@app_commands.describe(video_id="Video ID")
async def disableinterval(interaction: discord.Interaction, video_id: str):
    try:
        await db_execute("UPDATE intervals SET hours=0 WHERE video_id=?", (video_id,))
        await interaction.response.send_message("⏹️ Interval updates stopped")
    except:
        await interaction.response.send_message("❌ Failed to disable interval", ephemeral=True)

@bot.tree.command(name="setupcomingmilestonesalert", description="Upcoming milestones summary (<100K + ETA)")
@app_commands.describe(channel="Summary channel", ping="Optional ping message")
async def setupcomingmilestonesalert(interaction: discord.Interaction, channel: discord.TextChannel, ping: str = ""):
    try:
        await db_execute("INSERT OR REPLACE INTO upcoming_alerts (guild_id, channel_id, ping) VALUES (?, ?, ?)",
                        (str(interaction.guild.id), channel.id, ping))
        await interaction.response.send_message(f"📢 **<100K alerts + ETA** → <#{channel.id}>")
    except:
        await interaction.response.send_message("❌ Failed to setup alerts", ephemeral=True)

@bot.tree.command(name="upcoming", description="Current upcoming milestones (<100K + ETA)")
async def upcoming(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
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
            await interaction.followup.send(f"📊 **Upcoming (<100K)** ({now.strftime('%H:%M KST')}):
" + "
".join(lines))
        else:
            await interaction.followup.send("📭 No videos within 100K of milestones")
    except:
        await interaction.followup.send("❌ Failed to check upcoming")

@bot.tree.command(name="servercheck", description="Complete server tracking overview")
async def servercheck(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        guild_id = str(interaction.guild.id)
        videos = await db_execute("SELECT title, video_id, channel_id, alert_channel FROM videos WHERE guild_id=?", (guild_id,), True)
        milestones = await db_execute("SELECT v.title, m.last_million, m.ping FROM milestones m JOIN videos v ON m.video_id=v.video_id WHERE v.guild_id=?", (guild_id,), True)
        intervals = await db_execute("SELECT v.title, i.hours FROM intervals i JOIN videos v ON i.video_id=v.video_id WHERE v.guild_id=? AND i.hours > 0", (guild_id,), True)
        upcoming = await db_execute("SELECT channel_id, ping FROM upcoming_alerts WHERE guild_id=?", (guild_id,), True)
        
        response = f"**{interaction.guild.name} Overview** 📊

"
        if videos:
            response += "**📹 Videos:**
"
            for title, vid, ch_id, alert_ch in videos[:10]:
                ch = bot.get_channel(int(ch_id)).mention if bot.get_channel(int(ch_id)) else f"#{ch_id}"
                alert = bot.get_channel(int(alert_ch)).mention if bot.get_channel(int(alert_ch)) else f"#{alert_ch}"
                response += f"• **{title}** → {ch} | Alerts: {alert}
"
            if len(videos) > 10: response += f"...and {len(videos)-10} more
"
        else:
            response += "📭 No videos
"
        
        if milestones and any(m[1] > 0 for m in milestones):
            response += "
**📼 Milestones:**
" + "
".join(f"• **{t}**: {m}M" for t, m, _ in milestones if m > 0) + "
"
        
        if intervals:
            response += "
**⏱️ Intervals:**
" + "
".join(f"• **{t}**: {h}hr" for t, h in intervals) + "
"
        
        if upcoming:
            ch_id, ping = upcoming[0]
            ch = bot.get_channel(int(ch_id)).mention if bot.get_channel(int(ch_id)) else f"#{ch_id}"
            response += f"
**📢 Upcoming (<100K):** {ch} `{ping or 'No ping'}'"
        
        response += f"
**Total:** {len(videos)} videos | {len(intervals)} intervals"
        await interaction.followup.send(response)
    except:
        await interaction.followup.send("❌ Server check failed")

@bot.tree.command(name="botcheck", description="Bot status and timing verification")
async def botcheck(interaction: discord.Interaction):
    try:
        now = now_kst()
        vcount = len(await db_execute("SELECT * FROM videos",fetch=True))
        icount = len(await db_execute("SELECT * FROM intervals WHERE hours > 0",fetch=True))
        kst_status = "🟢" if kst_tracker.is_running() else "🔴"
        interval_status = "🟢" if tracking_loop.is_running() else "🔴"
        
        message = f"""✅ **KST**: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}
📊 **{vcount}** videos | **{icount}** intervals
🔄 **KST Tracker**: {kst_status} | **Interval**: {interval_status}
💾 **DB**: {DB_PATH}"""
        
        await interaction.response.send_message(message)
    except:
        await interaction.response.send_message("❌ Botcheck failed", ephemeral=True)

# ========================================
# ERROR HANDLER
# ========================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(f"⏳ Wait {error.retry_after:.1f}s", ephemeral=True)
    else:
        print(f"❌ Slash Error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Command failed", ephemeral=True)

# ========================================
# EVENTS
# ========================================
@bot.event
async def on_ready():
    await init_db()
    print(f"🚀 {bot.user} online - KST: {now_kst().strftime('%H:%M:%S')}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} commands globally")
    except Exception as e:
        print(f"❌ Sync failed: {e}")
    
    kst_tracker.start()
    tracking_loop.start()
    Thread(target=run_flask, daemon=True).start()
    print("🎯 PRODUCTION READY - All 16 commands + KST perfect timing!")

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
