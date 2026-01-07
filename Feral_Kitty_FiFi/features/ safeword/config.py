from typing import Any, Dict, List, Optional
from .constants import STAFF_FALLBACK_NAME

def sw_cfg(bot) -> Dict[str, Any]:
    return (getattr(bot, "config", None) or {}).get("safeword") or {}

def ensure_sw_cfg(bot) -> Dict[str, Any]:
    if not getattr(bot, "config", None):
        bot.config = {}
    if "safeword" not in bot.config:
        bot.config["safeword"] = {}
    return bot.config["safeword"]

def update_runtime_config(bot, guild, log_channel_id: int, responders_role: Optional[Any]):
    cfg = ensure_sw_cfg(bot)
    cfg.setdefault("enabled", True)
    cfg.setdefault("trigger", "!STOP!")
    cfg.setdefault("release_trigger", "!Release")
    cfg.setdefault("cooldown_seconds", 0)
    cfg["log_channel_id"] = log_channel_id

    rtps: List[Any] = cfg.get("roles_to_ping") or []
    wl: List[Any] = cfg.get("roles_whitelist") or [STAFF_FALLBACK_NAME]
    if responders_role and responders_role.id not in {rid for rid in rtps if isinstance(rid, int)}:
        rtps.append(responders_role.id)
    if responders_role and responders_role.id not in {rid for rid in wl if isinstance(rid, int)}:
        wl.append(responders_role.id)
    if STAFF_FALLBACK_NAME not in [r for r in wl if isinstance(r, str)]:
        wl.append(STAFF_FALLBACK_NAME)

    cfg["roles_to_ping"] = rtps
    cfg["roles_whitelist"] = wl
