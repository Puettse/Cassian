# cassian.py

import discord
import os
import json
import hashlib
from datetime import datetime
from supabase import create_client, Client
import requests

# Load config.json (relative path for Railway/GitHub)
with open("Config/config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

# Supabase init
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Utility to load file from local disk or GitHub URL
def load_file(path):
    try:
        if path.startswith("http://") or path.startswith("https://"):
            res = requests.get(path)
            if res.status_code == 200:
                return res.text.strip()
            else:
                print(f"[WARN] Failed to fetch remote file: {path} ({res.status_code})")
                return ""
        else:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception as e:
        print(f"[ERROR] Could not load file {path}: {e}")
        return ""

# Load memory layers dynamically
layers = {
    "DIRECTIVES": ("directives", "enable_directives"),
    "MEMORIES": ("memories", "enable_memories"),
    "BACKSTORY": ("backstory", "enable_backstory"),
    "EXAMPLES": ("examples", "enable_examples")
}

memory_chunks = []

for label, (path_key, toggle_key) in layers.items():
    if config.get(toggle_key, False) and config["paths"].get(path_key):
        content = load_file(config["paths"][path_key])
        if content:
            memory_chunks.append(f"[{label}]\n{content}")

system_message = "\n\n".join(memory_chunks)

# Discord setup
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

KINDROID_URL = config["kindroid"]["api_url"] + "/discord-bot"
KINDROID_KEY = os.getenv("KINDROID_API_KEY")
SHARE_CODE = config["kindroid"]["share_code"]
FILTER = config.get("enable_filter", True)

@client.event
async def on_ready():
    print(f"Cassian is online as {client.user}")

@client.event
async def on_message(message):
    if message.author == client.user or client.user not in message.mentions:
        return

    user_discord_id = str(message.author.id)
    username = message.author.display_name

    # Ensure user exists in Supabase
    user_data = supabase.table("users").select("id").eq("discord_id", user_discord_id).execute()
    if user_data.data:
        user_id = user_data.data[0]["id"]
        supabase.table("users").update({"last_seen": datetime.utcnow().isoformat()}).eq("id", user_id).execute()
    else:
        user_entry = supabase.table("users").insert({"discord_id": user_discord_id, "username": username}).execute()
        user_id = user_entry.data[0]["id"]

    # Hash username for requester header
    hashed_username = hashlib.sha256(username.encode()).hexdigest()[:32]

    # Build conversation payload
    conversation = [
        {
            "username": "System",
            "text": system_message,
            "timestamp": datetime.utcnow().isoformat()
        },
        {
            "username": username,
            "text": message.content,
            "timestamp": message.created_at.isoformat()
        }
    ]

    payload = {
        "share_code": SHARE_CODE,
        "enable_filter": FILTER,
        "conversation": conversation
    }

    headers = {
        "Authorization": f"Bearer {KINDROID_KEY}",
        "X-Kindroid-Requester": hashed_username
    }

    # Log prompt
    supabase.table("user_logs").insert({
        "user_id": user_id,
        "entry_type": "prompt",
        "content": message.content,
        "channel_id": str(message.channel.id),
        "message_id": str(message.id)
    }).execute()

    # Send to Kindroid
    try:
        res = requests.post(KINDROID_URL, json=payload, headers=headers)
        if res.status_code == 200:
            reply = res.text.strip()
            await message.channel.send(reply)

            # Log response
            supabase.table("user_logs").insert({
                "user_id": user_id,
                "entry_type": "response",
                "content": reply,
                "channel_id": str(message.channel.id),
                "message_id": str(message.id)
            }).execute()
        else:
            await message.channel.send("Cassian failed to respond.")
            print(f"[ERROR] Kindroid API returned {res.status_code}: {res.text}")
    except Exception as e:
        await message.channel.send("Cassian encountered an error.")
        print(f"[ERROR] {e}")

# Start bot
client.run(os.getenv("DISCORD_BOT_TOKEN"))
