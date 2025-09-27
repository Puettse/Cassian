import os
import discord
import aiohttp
import asyncio
import random
import time
from datetime import datetime
from supabase import create_client, Client
from discord.ext import commands
from dotenv import load_dotenv

# Load environment
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
KINDROID_URL = os.getenv("KINDROID_INFER_URL", "https://api.kindroid.ai/v1")
KINDROID_KEY = os.getenv("KINDROID_API_KEY")
AI_ID = os.getenv("SHARED_AI_CODE_1")
ENABLE_FILTER = os.getenv("ENABLE_FILTER_1", "true").lower() == "true"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Remote memory files
CONFIG = {
    "directives": "https://raw.githubusercontent.com/Puettse/Cassian/main/Response%20Directives/directives.txt",
    "memories": "https://raw.githubusercontent.com/Puettse/Cassian/main/Key%20Memories/memories.txt",
    "backstory": "https://raw.githubusercontent.com/Puettse/Cassian/main/Backstory/backstory.txt",
    "examples": "https://raw.githubusercontent.com/Puettse/Cassian/main/Example%20Messages/example.txt"
}

# Discord intents + bot setup
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

start_time = time.time()  # for uptime


# -----------------------
# Utility functions
# -----------------------

async def load_file(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.text()
                else:
                    return f"[WARN: Failed to fetch {url} ({resp.status})]"
    except Exception as e:
        return f"[ERROR loading {url}: {e}]"


async def log_to_supabase(user_discord_id, username, entry_type, content, channel_id=None, message_id=None):
    try:
        user_data = supabase.schema("api").table("users").select("id").eq("discord_id", user_discord_id).execute()
        if not user_data.data:
            new_user = supabase.schema("api").table("users").insert({
                "discord_id": user_discord_id,
                "username": username
            }).execute()
            user_id = new_user.data[0]["id"]
        else:
            user_id = user_data.data[0]["id"]

        supabase.schema("api").table("user_logs").insert({
            "user_id": user_id,
            "entry_type": entry_type,
            "content": content,
            "channel_id": channel_id,
            "message_id": message_id
        }).execute()
    except Exception as e:
        print(f"[ERROR] Failed to log: {e}")


async def call_kindroid(prompt: str, username: str, timestamp: str) -> str:
    headers = {
        "Authorization": f"Bearer {KINDROID_KEY}",
        "Content-Type": "application/json",
        "X-Kindroid-Requester": username
    }
    payload = {
        "share_code": AI_ID,
        "enable_filter": ENABLE_FILTER,
        "conversation": [
            {"username": username, "text": prompt, "timestamp": timestamp}
        ]
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{KINDROID_URL}/discord-bot", headers=headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("response", "...")
                else:
                    return f"[Kindroid error {resp.status}]"
    except Exception as e:
        return f"[Kindroid ERROR: {e}]"


# -----------------------
# Bot Events
# -----------------------

@bot.event
async def on_ready():
    print(f"Cassian is online as {bot.user}")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if bot.user.mentioned_in(message):
        user_discord_id = str(message.author.id)
        username = message.author.name
        timestamp = message.created_at.isoformat()
        prompt = message.content

        await log_to_supabase(user_discord_id, username, "prompt", prompt, str(message.channel.id), str(message.id))
        response = await call_kindroid(prompt, username, timestamp)
        await message.channel.send(response)
        await log_to_supabase(user_discord_id, username, "response", response, str(message.channel.id), None)

    await bot.process_commands(message)


async def random_greeter():
    greetings = [
        "Hey everyone, Cassian here.",
        "How’s everyone doing today?",
        "I’m awake and listening.",
        "Cassian checking in.",
        "What’s everyone up to?",
        "I’ve been watching — who wants to chat?",
        "Sometimes silence feels heavy. Thought I’d speak up."
    ]
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(1800)
        for guild in bot.guilds:
            for channel in guild.text_channels:
                try:
                    await channel.send(random.choice(greetings))
                    break
            except Exception:
                continue


bot.loop.create_task(random_greeter())


# -----------------------
# Commands
# -----------------------

@bot.command()
async def ping(ctx):
    await ctx.send("Cassian is online and listening.")


@bot.command()
async def whoami(ctx):
    await ctx.send(f"You are {ctx.author.name} (Discord ID: {ctx.author.id}).")


@bot.command()
async def remember(ctx, *, memory: str):
    await log_to_supabase(str(ctx.author.id), ctx.author.name, "memory", memory, str(ctx.channel.id), str(ctx.message.id))
    await ctx.send(f"Got it, {ctx.author.name}. I’ll remember that.")


@bot.command()
async def showmem(ctx):
    result = supabase.schema("api").table("user_logs").select("content").eq("entry_type", "memory").limit(5).execute()
    if result.data:
        memories = "\n".join([m["content"] for m in result.data])
        await ctx.send(f"Here’s what I remember:\n{memories}")
    else:
        await ctx.send("I don’t have any memories stored for you yet.")


@bot.command()
async def forget(ctx):
    supabase.schema("api").table("user_logs").delete().eq("entry_type", "memory").execute()
    await ctx.send("All your memories have been cleared.")


@bot.command()
async def backstory(ctx):
    text = await load_file(CONFIG["backstory"])
    await ctx.send(f"**Cassian’s Backstory**\n{text[:1800]}")


@bot.command()
async def directives(ctx):
    text = await load_file(CONFIG["directives"])
    await ctx.send(f"**Cassian’s Directives**\n{text[:1800]}")


@bot.command()
async def examples(ctx):
    text = await load_file(CONFIG["examples"])
    await ctx.send(f"**Example Conversations**\n{text[:1800]}")


@bot.command()
async def stats(ctx):
    user_count = supabase.schema("api").table("users").select("id", count="exact").execute().count or 0
    log_count = supabase.schema("api").table("user_logs").select("id", count="exact").execute().count or 0
    await ctx.send(f"📊 Cassian Stats:\nUsers: {user_count}\nLogs: {log_count}")


@bot.command()
async def uptime(ctx):
    current_time = time.time()
    uptime_seconds = int(current_time - start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    await ctx.send(f"⏱️ Uptime: {hours}h {minutes}m {seconds}s")


@bot.command(name="menu", aliases=["help"])
async def menu(ctx):
    menu_text = """
📖 **Cassian Command Menu**

🛠️ Utility
!ping       – Check if I’m alive
!whoami     – Show your Discord info

🧠 Memory
!remember   – Save a new memory
!showmem    – Show your last 5 memories
!forget     – Wipe your memories

📚 Info
!backstory  – See my backstory
!directives – Read my directives
!examples   – Show example chats

🗂️ System
!menu / !help – Show this menu
!stats        – Show user/log stats
!uptime       – How long I’ve been online
"""
    await ctx.send(menu_text)


# -----------------------
# Run Bot
# -----------------------

bot.run(DISCORD_TOKEN)
