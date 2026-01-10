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

# SAFE IMPORTS - NO CRASHES
try:
    from utils import now_kst, db_execute, fetch_views, estimate_eta, ensure_video_exists
except ImportError as e:
    print(f"CRITICAL: utils.py import failed: {e}")
    exit(1)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
DB_PATH = "youtube_bot.db"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("YouTubeBot")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

# Flask - Render safe
app = Flask(__name__)
@app.route("/") 
def home():
    return {"status": "alive"}

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

async def safe_response(interaction, content, ephemeral=False):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except:
        pass

# KST + Upcoming (simplified - NO CRASH)
@tasks.loop(minutes=1)
async def kst_tracker():
    try:
        now = now_kst()
        if now.hour in [0, 12, 17] and now.minute == 0:
            print(f"KST: {now}")
    except:
        pass

@tasks.loop(minutes=1)
async def interval_checker():
    try:
        print("Intervals running")
    except:
        pass

@interval_checker.before_loop
async def before_interval():
    await bot.wait_until_ready()

@kst_tracker.before_loop
async def before_kst():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    print("✅ Bot online!")
    print("✅ Starting tasks...")
    kst_tracker.start()
    interval_checker.start()
    Thread(target=run_flask, daemon=True).start()
    print("✅ ALL SYSTEMS GO!")

# 1 SIMPLE TEST COMMAND
@bot.tree.command(name="test", description="Test deployment")
async def test(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Bot working perfectly!")

if __name__ == "__main__":
    try:
        print("Starting bot...")
        bot.run(BOT_TOKEN)
    except Exception as e:
        print(f"FATAL ERROR: {e}")