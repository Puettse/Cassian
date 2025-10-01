  import os
import time
import random
import hashlib
from datetime import datetime, timezone

import aiohttp
import asyncio
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from supabase import create_client, Client

# ========== ENV ==========
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
KINDROID_URL = os.getenv("KINDROID_INFER_URL", "https://api.kindroid.ai/v1")
KINDROID_KEY = os.getenv("KINDROID_API_KEY")
AI_ID = os.getenv("SHARED_AI_CODE_1", "").strip()
ENABLE_FILTER = os.getenv("ENABLE_FILTER_1", "true").lower() == "true"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

MEMORY_URLS = {
    "directives": "https://raw.githubusercontent.com/Puettse/Cassian/main/Response%20Directives/directives.txt",
    "memories":   "https://raw.githubusercontent.com/Puettse/Cassian/main/Key%20Memories/memories.txt",
    "backstory":  "https://raw.githubusercontent.com/Puettse/Cassian/main/Backstory/backstory.txt",
    "examples":   "https://raw.githubusercontent.com/Puettse/Cassian/main/Example%20Messages/example.txt",
}

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

start_time = time.time()
SYSTEM_MESSAGE = ""


# ========== UTILITIES ==========

async def http_get_text(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=20) as resp:
                if resp.status == 200:
                    return await resp.text()
                print(f"[WARN] Failed to fetch remote file: {url} ({resp.status})")
                return ""
    except Exception as e:
        print(f"[ERROR] Could not load {url}: {e}")
        return ""

async def build_system_message() -> str:
    directives = await http_get_text(MEMORY_URLS["directives"])
    memories   = await http_get_text(MEMORY_URLS["memories"])
    backstory  = await http_get_text(MEMORY_URLS["backstory"])
    examples   = await http_get_text(MEMORY_URLS["examples"])

    parts = []
    if directives.strip():
        parts.append(f"[DIRECTIVES]\n{directives.strip()}")
    if memories.strip():
        parts.append(f"[MEMORIES]\n{memories.strip()}")
    if backstory.strip():
        parts.append(f"[BACKSTORY]\n{backstory.strip()}")
    if examples.strip():
        parts.append(f"[EXAMPLES]\n{examples.strip()}")

    return "\n\n".join(parts).strip()

async def ensure_user_and_log(user_discord_id, username, entry_type, content,
                              channel_id=None, message_id=None, visible=None):
    try:
        ures = supabase.schema("api").table("users").select("id").eq("discord_id", user_discord_id).execute()
        if not ures.data:
            ins = supabase.schema("api").table("users").insert({
                "discord_id": user_discord_id,
                "username": username
            }).execute()
            user_id = ins.data[0]["id"]
        else:
            user_id = ures.data[0]["id"]

        payload = {
            "user_id": user_id,
            "entry_type": entry_type,
            "content": content,
            "channel_id": channel_id,
            "message_id": message_id,
        }
        if entry_type == "memory":
            payload["visible"] = True if visible is None else visible

        supabase.schema("api").table("user_logs").insert(payload).execute()
        return user_id
    except Exception as e:
        print(f"[ERROR] Failed to log to Supabase: {e}")
        return None

async def call_kindroid(conversation: list[dict], requester_hint: str) -> str:
    requester = hashlib.sha256(requester_hint.encode("utf-8")).hexdigest()[:32]
    headers = {
        "Authorization": f"Bearer {KINDROID_KEY}",
        "Content-Type": "application/json",
        "X-Kindroid-Requester": requester,
    }
    payload = {
        "share_code": AI_ID or "",
        "enable_filter": ENABLE_FILTER,
        "conversation": conversation,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{KINDROID_URL}/discord-bot", headers=headers, json=payload, timeout=90) as resp:
                data = await resp.json()
                return data.get("reply", "[ERROR] No reply received.")
    except Exception as e:
        return f"[Kindroid ERROR: {e}]"


# ========== EVENTS ==========

@bot.event
async def on_ready():
    global SYSTEM_MESSAGE
    SYSTEM_MESSAGE = await build_system_message()
    print(f"Cassian is online as {bot.user}")
    if not random_greeter.is_running():
        random_greeter.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if bot.user.mentioned_in(message):
        user_discord_id = str(message.author.id)
        username = message.author.name
        ts = message.created_at.isoformat()
        prompt_text = message.content

        await ensure_user_and_log(user_discord_id, username, "prompt", prompt_text, str(message.channel.id), str(message.id))

        conversation = []
        if SYSTEM_MESSAGE:
            conversation.append({"username": "System", "text": SYSTEM_MESSAGE, "timestamp": datetime.now(timezone.utc).isoformat()})
        conversation.append({"username": username, "text": prompt_text, "timestamp": ts})

        reply = await call_kindroid(conversation, requester_hint=username)
        await message.channel.send(reply)

        await ensure_user_and_log(user_discord_id, username, "response", reply, str(message.channel.id), None)

    await bot.process_commands(message)


# ========== GREETER ==========

@tasks.loop(minutes=30)
async def random_greeter():
    greetings = [
        "Hey everyone, Cassian here.",
        "How’s everyone doing today?",
        "I’m awake and listening.",
        "Cassian checking in.",
        "What’s everyone up to?",
        "I’ve been watching — who wants to chat?",
        "Sometimes silence feels heavy. Thought I’d speak up.",
    ]
    for guild in bot.guilds:
        for channel in getattr(guild, "text_channels", []):
            try:
                await channel.send(random.choice(greetings))
                break
            except Exception as e:
                print(f"[WARN] random_greeter failed in {getattr(channel, 'id', '?')}: {e}")
                continue

@random_greeter.before_loop
async def _greeter_ready():
    await bot.wait_until_ready()


# ========== COMMANDS ==========

@bot.command()
async def ping(ctx):
    await ctx.send("Cassian is online and listening.")

@bot.command()
async def whoami(ctx):
    await ctx.send(f"You are {ctx.author.name} (Discord ID: {ctx.author.id}).")

@bot.command()
async def remember(ctx, *, memory: str):
    await ensure_user_and_log(str(ctx.author.id), ctx.author.name, "memory", memory, str(ctx.channel.id), str(ctx.message.id), visible=True)
    await ctx.send(f"Got it, {ctx.author.name}. I’ll remember that just for you.")

@bot.command()
async def showmem(ctx):
    user_discord_id = str(ctx.author.id)
    ures = supabase.schema("api").table("users").select("id").eq("discord_id", user_discord_id).execute()
    if not ures.data:
        await ctx.send("I don’t have any memories stored for you yet.")
        return
    user_id = ures.data[0]["id"]
    q = supabase.schema("api").table("user_logs").select("id, content, created_at").eq("user_id", user_id).eq("entry_type", "memory").eq("visible", True).order("created_at", desc=True).limit(5).execute()
    if q.data:
        lines = [f"{i+1}. {row['content']} ({row['created_at']})" for i, row in enumerate(q.data)]
        await ctx.send("Here are your recent memories:\n" + "\n".join(lines))
    else:
        await ctx.send("I don’t have any visible memories stored for you yet.")

@bot.command()
async def purge_last(ctx, number: int):
    user_discord_id = str(ctx.author.id)
    ures = supabase.schema("api").table("users").select("id").eq("discord_id", user_discord_id).execute()
    if not ures.data:
        await ctx.send("No memories found for you.")
        return
    user_id = ures.data[0]["id"]
    q = supabase.schema("api").table("user_logs").select("id").eq("user_id", user_id).eq("entry_type", "memory").eq("visible", True).order("created_at", desc=True).limit(number).execute()
    if not q.data:
        await ctx.send("No visible memories to purge.")
        return
    ids = [row["id"] for row in q.data]
    supabase.schema("api").table("user_logs").update({"visible": False}).in_("id", ids).execute()
    await ctx.send(f"Purged last {len(ids)} memories from your view.")

@bot.command()
async def purge_mem(ctx, index: int):
    user_discord_id = str(ctx.author.id)
    ures = supabase.schema("api").table("users").select("id").eq("discord_id", user_discord_id).execute()
    if not ures.data:
        await ctx.send("No memories found for you.")
        return
    user_id = ures.data[0]["id"]
    q = supabase.schema("api").table("user_logs").select("id, content").eq("user_id", user_id).eq("entry_type", "memory").eq("visible", True).order("created_at", desc=True).execute()
    if not q.data or index < 1 or index > len(q.data):
        await ctx.send("Invalid memory index.")
        return
    target_id = q.data[index - 1]["id"]
    supabase.schema("api").table("user_logs").update({"visible": False}).eq("id", target_id).execute()
    await ctx.send(f"Purged memory #{index} from your view.")

@bot.command()
async def backstory(ctx):
    txt = await http_get_text(MEMORY_URLS["backstory"])
    await ctx.send(f"**Cassian’s Backstory**\n{txt[:1800]}" if txt else "Backstory not available.")

@bot.command()
async def directives(ctx):
    txt = await http_get_text(MEMORY_URLS["directives"])
    await ctx.send(f"**Cassian’s Directives**\n{txt[:1800]}" if txt else "Directives not available.")

@bot.command()
async def examples(ctx):
    txt = await http_get_text(MEMORY_URLS["examples"])
    await ctx.send(f"**Example Conversations**\n{txt[:1800]}" if txt else "Examples not available.")

@bot.command()
async def stats(ctx):
    users = supabase.schema("api").table("users").select("id", count="exact").execute()
    logs  = supabase.schema("api").table("user_logs").select("id", count="exact").execute()
    await ctx.send(f"📊 Cassian Stats:\nUsers: {users.count or 0}\nLogs: {logs.count or 0}")

@bot.command()
async def uptime(ctx):
    secs = int(time.time() - start_time)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    await ctx.send(f"⏱️ Uptime: {h}h {m}m {s}s")

@bot.command(name="menu", aliases=["help"])
async def menu(ctx):
    await ctx.send(
        """
📖 **Cassian Command Menu**

🛠️ Utility
!ping          – Check if I’m alive
!whoami        – Show your Discord info

🧠 Memory
!remember <t>  – Save a new memory
!showmem       – Show your last 5 memories
!purge_last X  – Hide your last X memories
!purge_mem N   – Hide memory #N

🤗 Actions
!hug           – Receive a warm hug
!headpat       – Gentle headpats
!kiss          – A sweet, safe kiss
!uppies        – Picked up lovingly
!snuggle       – Wrapped in comfort
!tuckin        – Tucked in safe and sound

📚 Info
!backstory     – My backstory
!directives    – My inner logic
!examples      – Example convos

🗂️ System
!menu / !help  – Show this menu
!stats         – User/log stats
!uptime        – Time since launch
"""
    )


# ========== SFW ACTIONS ==========

action_responses = {
    "hug": [
        "wraps arms around {user} in a warm, safe hug.",
        "gently pulls {user} into a bear hug.",
        "opens arms wide for {user} — come here, let’s hug it out.",
    ],
    "headpat": [
        "places a gentle hand on {user}’s head and gives a few soft pats.",
        "ruffles {user}’s hair affectionately.",
        "gives {user} a reassuring pat on the head.",
    ],
    "kiss": [
        "presses a soft kiss to {user}’s forehead.",
        "gives {user} a sweet little kiss on the cheek.",
        "leans in and leaves a gentle kiss on {user}’s brow.",
    ],
    "uppies": [
        "scoops {user} up into strong arms — uppies granted!",
        "lifts {user} gently and securely.",
        "offers {user} a ride in my arms. Up you go!",
    ],
    "snuggle": [
        "pulls {user} close into a long, comforting snuggle.",
        "wraps around {user} like a warm blanket.",
        "settles down beside {user} for a cozy snuggle session.",
    ],
    "tuckin": [
        "fluffs the pillows and gently tucks {user} into bed.",
        "draws the blanket up around {user} with a tender smile.",
        "whispers goodnight as {user} gets tucked in safe and sound.",
    ]
}

def register_action_commands(bot):
    for action, lines in action_responses.items():
        async def command_func(ctx, action=action, lines=lines):
            line = random.choice(lines).replace("{user}", ctx.author.mention)
            await ctx.send(line)

        bot.command(name=action)(command_func)

register_action_commands(bot)

# ========== RUN ==========
bot.run(DISCORD_TOKEN)
