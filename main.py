import discord
from discord.ext import commands, tasks
import os
import asyncio
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
from datetime import datetime, timedelta
import logging

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
@app.route('/')
def home():
    return "alive"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

@tasks.loop(minutes=1)
async def heartbeat():
    print("Bot alive")

@bot.event
async def on_ready():
    log.info(f'{bot.user} online!')
    heartbeat.start()
    Thread(target=run_flask, daemon=True).start()
    synced = await bot.tree.sync()
    log.info(f'Synced {len(synced)} commands')

@bot.tree.command(name="ping")
async def ping(interaction):
    await interaction.response.send_message("✅ Bot working!")

if __name__ == "__main__":
    bot.run(TOKEN)