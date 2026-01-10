import discord
from discord.ext import commands
import os
from flask import Flask
from threading import Thread
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

app = Flask(__name__)
@app.route('/')
def home():
    return "alive"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

@bot.event
async def on_ready():
    print(f'{bot.user} online!')
    print('Bot LIVE!')
    Thread(target=run_flask, daemon=True).start()

@bot.tree.command(name="ping")
async def ping(interaction):
    await interaction.response.send_message("✅ WORKING!")

if __name__ == "__main__":
    bot.run(TOKEN)