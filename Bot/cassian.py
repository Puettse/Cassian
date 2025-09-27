# cassian.py

import discord
import os
import json
import asyncio
import hashlib
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client, Client
import requests

# Load .env
load_dotenv("C:\\Users\\sethp\\Desktop\\Cassian\\Security\\cassian.env")

# Load config.json
with open("C:\\Users\\sethp\\Desktop\\Cassian\\Config\\config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

# Supabase init
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Load memory layers
def load_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

directives = load_file(config["paths"].get("directives")) if config.get("enable_directives", True) else ""
memories   = load_file(config["paths"].get("memories")) if config.get("enable_memories", True) else ""
backstory  = load_file(config["paths"].get("backstory")) if config.get("enable_backstory", True) else ""
examples   = load_file("C:\\Users\\sethp\\Desktop\\Cassian\\Example Messages\\example.txt") if config.get("enable_examples", True) else ""

system_message = "\n\n".join(filter(None, [
    f"[DIRECTIVES]\n{directives}" if directives else "",
    f"[MEMORIES]\n{memories}" if memories else "",
    f"[BACKSTORY]\n{backstory}" if backstory else "",
    f"[EXAMPLES]\n{examples}" if examples else ""
]))

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

    # Insert or update user in Supabase
    user_data = supabase.table("users").select("id").eq("discord_id", user_discord_id).execute()
    if user_data.data:
        user_id = user_data.data[0]['id']
        supabase.table("users").update({"last_seen": datetime.utcnow().isoformat()}).eq("id", user_id).execute()
    else:
        user_entry = supabase.table("users").insert({"discord_id": user_discord_id, "username": username}).execute()
        user_id = user_entry.data[0]['id']

    # Prepare conversation payload
    hashed_username = hashlib.sha256(username.encode()).hexdigest()[:32]
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
    except Exception as e:
        await message.channel.send("Cassian encountered an error.")
        print(e)

# Start bot
client.run(os.getenv("DISCORD_BOT_TOKEN"))