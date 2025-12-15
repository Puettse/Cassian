import os
import json
import asyncio
from typing import Any, Dict
from .io_types import JSONDict

CONFIG_PATH = os.environ.get("FKF_CONFIG_PATH", "data/config.json")

DEFAULT_CFG: JSONDict = {
    "renames": {},
    "purge": [],
    "roles": [],
    "safeword": {
        "enabled": True,
        "trigger": "!STOP!",
        "release_trigger": "!Release",
        "log_channel_id": 1400887461907009666,
        "history_limit": 25,
        "roles_to_ping": ["Staff","SECURITY","The Enforcer","Watcher","Red Guard","The Father"],
        "roles_whitelist": ["Staff"],
        "blocked_roles": ["jailed"],
        "cooldown_seconds": 30,
        "lock_message": {"text": "!STOP! HAS BEEN CALLED; CHANNEL IS LOCKED PENDING REVIEW, PLEASE STANDBY. THANK YOU FOR YOUR PATIENCE.","image_url": ""},
        "release_message": {"text": "Channel released. Please continue respectfully.","image_url": ""}
    },
    "reaction_panels": []
}

_lock = asyncio.Lock()

def _deep_merge(dst: JSONDict, src: JSONDict) -> JSONDict:
    # Only merge known top-level sections; shallow is enough for this schema
    merged = json.loads(json.dumps(dst))
    merged.update(src or {})
    if "safeword" in src:
        merged["safeword"].update(src["safeword"] or {})
    if "reaction_panels" not in merged:
        merged["reaction_panels"] = []
    return merged

async def load_config() -> JSONDict:
    if not os.path.exists(CONFIG_PATH):
        async with _lock:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CFG, f, ensure_ascii=False, indent=2)
        return json.loads(json.dumps(DEFAULT_CFG))
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return _deep_merge(DEFAULT_CFG, raw)

async def save_config(cfg: JSONDict) -> None:
    async with _lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
