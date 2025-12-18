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

# Feral_Kitty_FiFi/main.py (snippet): add your new module path to EXTENSIONS
EXTENSIONS = [
    "Feral_Kitty_FiFi.features.safeword",
    "Feral_Kitty_FiFi.features.roles_build",
    "Feral_Kitty_FiFi.features.reaction_panels",
    "Feral_Kitty_FiFi.features.member_console",
    "Feral_Kitty_FiFi.features.admin",
    "Feral_Kitty_FiFi.features.help",
    "Feral_Kitty_FiFi.features.reminders",  # ← add this line
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

