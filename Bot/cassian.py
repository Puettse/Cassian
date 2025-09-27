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

# ========= Environment =========
load_dotenv()  # harmless on Railway; useful locally

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Kindroid
KINDROID_URL = os.getenv("KINDROID_INFER_URL", "https://api.kindroid.ai/v1")
KINDROID_KEY = os.getenv("KINDROID_API_KEY")
AI_ID = os.getenv("SHARED_AI_CODE_1")
ENABLE_FILTER = os.getenv("ENABLE_FILTER_1", "true").lower() == "true"

# Supabase (use service role key; our Railway var name is SUPABASE_KEY)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")  # <-- IMPORTANT: consistent with your Railway vars

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Remote memory files (now public)
MEMORY_URLS = {
    "directives": "https://raw.githubusercontent.com/Puettse/Cassian/main/Response%20Directives/directives.txt",
    "memories":   "https://raw.githubusercontent.com/Puettse/Cassian/main/Key%20Memories/memories.txt",
    "backstory":  "https://raw.githubusercontent.com/Puettse/Cassian/main/Backstory/backstory.txt",
    "examples":   "https://raw.githubusercontent.com/Puettse/Cassian/main/Example%20Messages/example.txt",
}

# Discord
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

start_time = time.time()
SYSTEM_MESSAGE = ""  # populated on_ready()


# ========= Utilities =========

async def http_get_text(url: str) -> str:
    """Fetch text from a URL (used for GitHub raw memory layers)."""
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
    """Assemble the composite system prompt from all 4 memory layers."""
    directives = await http_get_text(MEMORY_URLS["directives"])
    memories   = await http_get_text(MEMORY_URLS["memories"])
    backstory  = await http_get_text(MEMORY_URLS["backstory"])
    examples   = await http_get_text(MEMORY_URLS["examples"])

    parts = []
    if directives: parts.append(f"[DIRECTIVES]\n{directives.strip()}")
    if memories:   parts.append(f"[MEMORIES]\n{memories.strip()}")
    if backstory:  parts.append(f"[BACKSTORY]\n{backstory.strip()}")
    if examples:   parts.append(f"[EXAMPLES]\n{examples.strip()}")

    return "\n\n".join(parts).strip()

async def ensure_user_and_log(user_discord_id: str, username: str, entry_type: str,
                              content: str, channel_id: str | None, message_id: str | None,
                              visible: bool | None = None):
    """Ensure user row exists in api.users, then insert a row into api.user_logs."""
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
            "message_id": message_id
        }
        # Only include `visible` field for memory entries; leave it out for others.
        if entry_type == "memory":
            payload["visible"] = True if visible is None else visible

        supabase.schema("api").table("user_logs").insert(payload).execute()
        return user_id
    except Exception as e:
        print(f"[ERROR] Failed to log to Supabase: {e}")
        return None

async def call_kindroid(conversation: list[dict], requester_token: str) -> str:
    """Call Kindroid /discord-bot endpoint with a conversation array and return text."""
    headers = {
        "Authorization": f"Bearer {KINDROID_KEY}",
        "Content-Type": "application/json",
        "X-Kindroid-Requester": requester_token[:32]  # simple limiter
    }
    payload = {
        "share_code": AI_ID,
        "enable_filter": ENABLE_FILTER,
        "conversation": conversation
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{KINDROID_URL}/discord-bot", headers=headers, json=payload, timeout=60) as resp:
                # Kindroid doc says 200 OK with response; treat body as text to be safe
                txt = await resp.text()
                if resp.status == 200:
                    return txt.strip() if txt.strip() else "..."
                return f"[Kindroid error {resp.status}] {txt[:400]}"
    except Exception as e:
        return f"[Kindroid ERROR: {e}]"


# ========= Events =========

@bot.event
async def on_ready():
    global SYSTEM_MESSAGE
    SYSTEM_MESSAGE = await build_system_message()
    print(f"Cassian is online as {bot.user}")
    if not random_greeter.is_running():
        random_greeter.start()

@bot.event
async def on_message(message: discord.Message):
    # Ignore self
    if message.author == bot.user:
        return

    # Independent reply when mentioned
    if bot.user.mentioned_in(message):
        user_discord_id = str(message.author.id)
        username = message.author.name
        ts = message.created_at.isoformat()
        prompt_text = message.content

        await ensure_user_and_log(
            user_discord_id, username, "prompt", prompt_text,
            str(message.channel.id), str(message.id)
        )

        # Build conversation with our system layer
        conversation = []
        if SYSTEM_MESSAGE:
            conversation.append({
                "username": "System",
                "text": SYSTEM_MESSAGE,
                "timestamp": datetime.utcnow().isoformat()
            })
        conversation.append({
            "username": username,
            "text": prompt_text,
            "timestamp": ts
        })

        reply = await call_kindroid(conversation, requester_token=username)
        await message.channel.send(reply)

        await ensure_user_and_log(
            user_discord_id, username, "response", reply,
            str(message.channel.id), None
        )

    # Let commands still work
    await bot.process_commands(message)


# ========= Background: random greeter =========

@discord.ext.tasks.loop(minutes=30)
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
    try:
        for guild in bot.guilds:
            for channel in guild.text_channels:
                try:
                    await channel.send(random.choice(greetings))
                    break  # one channel per guild per tick
    except Exception as e:
        print(f"[WARN] random_greeter: {e}")


# ========= Commands =========

@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send("Cassian is online and listening.")

@bot.command()
async def whoami(ctx: commands.Context):
    await ctx.send(f"You are {ctx.author.name} (Discord ID: {ctx.author.id}).")

# ---- Memories (per-user, soft delete) ----

@bot.command()
async def remember(ctx: commands.Context, *, memory: str):
    """Save a private memory (visible only to the user)."""
    await ensure_user_and_log(
        str(ctx.author.id), ctx.author.name, "memory", memory,
        str(ctx.channel.id), str(ctx.message.id), visible=True
    )
    await ctx.send(f"Got it, {ctx.author.name}. I’ll remember that just for you.")

@bot.command()
async def showmem(ctx: commands.Context):
    """Show last 5 visible memories for the invoking user."""
    user_discord_id = str(ctx.author.id)
    ures = supabase.schema("api").table("users").select("id").eq("discord_id", user_discord_id).execute()
    if not ures.data:
        await ctx.send("I don’t have any memories stored for you yet.")
        return
    user_id = ures.data[0]["id"]

    q = (supabase.schema("api").table("user_logs")
         .select("id, content, created_at")
         .eq("user_id", user_id)
         .eq("entry_type", "memory")
         .eq("visible", True)
         .order("created_at", desc=True)
         .limit(5)
         .execute())

    if q.data:
        lines = [f"{i+1}. {row['content']} ({row['created_at']})" for i, row in enumerate(q.data)]
        await ctx.send("Here are your recent memories:\n" + "\n".join(lines))
    else:
        await ctx.send("I don’t have any visible memories stored for you yet.")

@bot.command()
async def purge_last(ctx: commands.Context, number: int):
    """Hide (soft-delete) the last X visible memories for the user."""
    user_discord_id = str(ctx.author.id)
    ures = supabase.schema("api").table("users").select("id").eq("discord_id", user_discord_id).execute()
    if not ures.data:
        await ctx.send("No memories found for you.")
        return
    user_id = ures.data[0]["id"]

    q = (supabase.schema("api").table("user_logs")
         .select("id")
         .eq("user_id", user_id)
         .eq("entry_type", "memory")
         .eq("visible", True)
         .order("created_at", desc=True)
         .limit(number)
         .execute())
    if not q.data:
        await ctx.send("No visible memories to purge.")
        return

    ids = [r["id"] for r in q.data]
    supabase.schema("api").table("user_logs").update({"visible": False}).in_("id", ids).execute()
    await ctx.send(f"Purged last {len(ids)} memories from your view.")

@bot.command()
async def purge_mem(ctx: commands.Context, index: int):
    """Hide (soft-delete) memory #N from the user's visible list."""
    user_discord_id = str(ctx.author.id)
    ures = supabase.schema("api").table("users").select("id").eq("discord_id", user_discord_id).execute()
    if not ures.data:
        await ctx.send("No memories found for you.")
        return
    user_id = ures.data[0]["id"]

    allq = (supabase.schema("api").table("user_logs")
            .select("id, content")
            .eq("user_id", user_id)
            .eq("entry_type", "memory")
            .eq("visible", True)
            .order("created_at", desc=True)
            .execute())
    if not allq.data or index < 1 or index > len(allq.data):
        await ctx.send("Invalid memory index.")
        return

    target_id = allq.data[index - 1]["id"]
    supabase.schema("api").table("user_logs").update({"visible": False}).eq("id", target_id).execute()
    await ctx.send(f"Purged memory #{index} from your view.")

# ---- Info ----

@bot.command()
async def backstory(ctx: commands.Context):
    txt = await http_get_text(MEMORY_URLS["backstory"])
    await ctx.send(f"**Cassian’s Backstory**\n{txt[:1800]}" if txt else "Backstory not available.")

@bot.command()
async def directives(ctx: commands.Context):
    txt = await http_get_text(MEMORY_URLS["directives"])
    await ctx.send(f"**Cassian’s Directives**\n{txt[:1800]}" if txt else "Directives not available.")

@bot.command()
async def examples(ctx: commands.Context):
    txt = await http_get_text(MEMORY_URLS["examples"])
    await ctx.send(f"**Example Conversations**\n{txt[:1800]}" if txt else "Examples not available.")

# ---- System ----

@bot.command()
async def stats(ctx: commands.Context):
    users = supabase.schema("api").table("users").select("id", count="exact").execute()
    logs  = supabase.schema("api").table("user_logs").select("id", count="exact").execute()
    user_count = users.count or 0
    log_count  = logs.count or 0
    await ctx.send(f"📊 Cassian Stats:\nUsers: {user_count}\nLogs: {log_count}")

@bot.command()
async def uptime(ctx: commands.Context):
    secs = int(time.time() - start_time)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    await ctx.send(f"⏱️ Uptime: {h}h {m}m {s}s")

@bot.command(name="menu", aliases=["help"])
async def menu(ctx: commands.Context):
    await ctx.send(
        """
📖 **Cassian Command Menu**

🛠️ Utility
!ping          – Check if I’m alive
!whoami        – Show your Discord info

🧠 Memory (private to you; soft-delete on purge)
!remember <t>  – Save a new memory
!showmem       – Show your last 5 memories
!purge_last X  – Hide your last X memories
!purge_mem N   – Hide memory #N from your list

📚 Info
!backstory     – See my backstory
!directives    – Read my directives
!examples      – Show example chats

🗂️ System
!menu / !help  – Show this menu
!stats         – Show user/log stats
!uptime        – How long I’ve been online
"""
    )

# ========= Run =========
bot.run(DISCORD_TOKEN)
