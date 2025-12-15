# feral_kitty_fifi/main.py
import os
import logging
import discord
from discord.ext import commands
from .logging_setup import init_logging
from .config import load_config

init_logging()
log = logging.getLogger("Feral_Kitty_FiFi")

TOKEN = os.environ["DISCORD_TOKEN"]  # set in Railway Variables

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

EXTENSIONS = [
    "feral_kitty_fifi.features.safeword",
    "feral_kitty_fifi.features.roles_build",
    "feral_kitty_fifi.features.reaction_panels",
    "feral_kitty_fifi.features.member_console",
    "feral_kitty_fifi.features.admin",
    "feral_kitty_fifi.features.help",
]

@bot.event
async def setup_hook():
    bot.config = await load_config()
    for ext in EXTENSIONS:
        await bot.load_extension(ext)
    log.info("Extensions loaded: %s", ", ".join(EXTENSIONS))

@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)  # type: ignore

bot.run(TOKEN)

