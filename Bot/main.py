# Cassian Response Engine (Core)

import os
import requests
import json
from datetime import datetime
from supabase import create_client, Client

# --------------------
# 0. ENV Setup (Railway)
# --------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE")
KINDROID_API_KEY = os.environ.get("KINDROID_API_KEY")
KINDROID_AI_ID = os.environ.get("KINDROID_AI_ID")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --------------------
# 1. Load Core Prompts
# --------------------
def load_text(name):
    with open(name, 'r', encoding='utf-8') as f:
        return f.read().strip()

backstory = load_text("backstory.txt")
directives = load_text("directives.txt")
examples = load_text("example.txt")

# --------------------
# 2. Emotion Detection
# --------------------
def detect_emotional_state(message):
    lowered = message.lower()
    if any(x in lowered for x in ["quiet", "shut down"]): return "nonverbal"
    if any(x in lowered for x in ["overwhelmed", "too much"]): return "overstimulated"
    if any(x in lowered for x in ["panic", "can’t breathe"]): return "panic"
    if any(x in lowered for x in ["regress", "blankie"]): return "regression"
    if any(x in lowered for x in ["adhd", "tabs"]): return "adhd/distracted"
    if any(x in lowered for x in ["play", "castle"]): return "playful"
    if any(x in lowered for x in ["bed", "tired", "sleep"]): return "bedtime"
    return "neutral"

# --------------------
# 3. Memory Recall
# --------------------
def recall_memory(user_id):
    try:
        response = supabase.table("conversations").select("text").eq("user_id", user_id).limit(5).execute()
        logs = [row['text'] for row in response.data]
        return "\n".join(logs)
    except Exception as e:
        print("[Memory Error]", e)
        return ""

# --------------------
# 4. Build Prompt
# --------------------
def build_prompt(user_input, user_id):
    state = detect_emotional_state(user_input)
    memory = recall_memory(user_id)
    return f"""
You are Cassian Roe.

Backstory:
{backstory}

Directives:
{directives}

State: {state}

Examples:
{examples}

Previous Memory:
{memory}

User: "{user_input}"
Cassian:
"""

# --------------------
# 5. Call Kindroid
# --------------------
def send_to_kindroid(prompt):
    url = "https://api.kindroid.ai/v1/send-message"
    headers = {
        "Authorization": f"Bearer {KINDROID_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "ai_id": KINDROID_AI_ID,
        "message": prompt
    }
    response = requests.post(url, headers=headers, json=data)
    if response.status_code != 200:
        print("[Kindroid Error]", response.status_code, response.text)
        return None
    return response.json().get("reply")

# --------------------
# 6. Logging
# --------------------
def log_to_supabase(user_id, username, text):
    try:
        supabase.table("conversations").insert({
            "user_id": user_id,
            "username": username,
            "text": text,
            "timestamp": datetime.now().isoformat()
        }).execute()
    except Exception as e:
        print("[Supabase Log Error]", e)

# --------------------
# 7. Handle Message
# --------------------
def handle_message(user_id, username, message):
    prompt = build_prompt(message, user_id)
    reply = send_to_kindroid(prompt)
    log_to_supabase(user_id, username, message)
    log_to_supabase(user_id, "Cassian", reply)
    return reply

# --------------------
# Sample Run
# --------------------
if __name__ == "__main__":
    print(handle_message("test_user_1", "Sunshine", "hi cassian... my hands are shaking again"))
