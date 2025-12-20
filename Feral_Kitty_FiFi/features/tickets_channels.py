# Feral_Kitty_FiFi/features/tickets_channels.py
from __future__ import annotations

import io
import json
import html
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any
from ..config import save_config

try:
    import openpyxl
    from openpyxl.styles import Font
except Exception:
    openpyxl = None


# ----------------------------
# Small utils
# ----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ts_fmt(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M")


def yyyymm(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return f"{dt.year:04d}{dt.month:02d}"


def parse_hex_color(s: str, default: int = 0x5865F2) -> int:
    try:
        s = (s or "").strip()
        if not s:
            return default
        s = s.lower().replace("#", "").replace("0x", "")
        return int(s, 16)
    except Exception:
        return default


def safe_join_mentions(items: List[str], limit: int = 1800) -> str:
    out = []
    total = 0
    for it in items:
        if not it:
            continue
        if total + len(it) + 1 > limit:
            break
        out.append(it)
        total += len(it) + 1
    return " ".join(out)


def _as_text_or_file(text: str) -> Tuple[Optional[str], Optional[discord.File]]:
    if len(text) <= 1900:
        return text, None
    buf = io.BytesIO(text.encode("utf-8"))
    buf.seek(0)
    return "📄 Output was long; attached as file.", discord.File(buf, filename="output.txt")


# ----------------------------
# Config helpers
# ----------------------------
def tickets_cfg(bot: commands.Bot) -> Dict[str, Any]:
    cfg = bot.config.setdefault("tickets", {})
    cfg.setdefault("log_channel_id", None)
    cfg.setdefault("staff_role_ids", [])
    cfg.setdefault("roles_to_ping_names", [])
    cfg.setdefault("panel", {
        "hub_channel_id": None,
        "image_url": "",
        "title": "How can we help?",
        "description": "Pick a category below to open a ticket channel.",
        "colors": ["#5865F2"],
    })
    cfg.setdefault("panel_options", [])
    cfg.setdefault("delete_images", {"scan_limit": 500})
    cfg.setdefault("counters", {})
    cfg.setdefault("active", {})
    cfg.setdefault("records", [])
    cfg.setdefault("allow_user_close", False)
    cfg.setdefault("archive", {"enabled": True, "category_id": None, "rename_prefix": "closed-"})
    cfg.setdefault("transcripts", {"format": "html"})
    _ensure_ticket_defaults(cfg)
    return cfg


def _ensure_ticket_defaults(cfg: Dict[str, Any]) -> None:
    """Ensure all default ticket options exist once and never get overwritten."""
    if "panel_options" not in cfg or not isinstance(cfg["panel_options"], list):
        cfg["panel_options"] = []

    existing = {str(o.get("value", "")).lower() for o in cfg["panel_options"]}

    defaults = [
        # --- Verification ---
        {
            "label": "ID VERIFY",
            "value": "id_verification",
            "code": "IDV",
            "parent_category_id": None,
            "emoji": "🪪",
            "description": "Upload a valid ID for age verification.",
            "verification": True,
            "open_voice": False,
            "staff_role_ids": [],
        },
        {
            "label": "CROSS VERIFY",
            "value": "cross_verification",
            "code": "XVER",
            "parent_category_id": None,
            "emoji": "🧩",
            "description": "Request cross verification from another server.",
            "verification": True,
            "open_voice": False,
            "staff_role_ids": [],
        },
        {
            "label": "VC VERIFY",
            "value": "video_verification",
            "code": "VVER",
            "parent_category_id": None,
            "emoji": "🎥",
            "description": "Verify your age over a quick video call.",
            "verification": True,
            "open_voice": True,
            "staff_role_ids": [],
        },
        # --- Support / Non-verification ---
        {
            "label": "REPORT",
            "value": "report",
            "code": "RPT",
            "parent_category_id": None,
            "emoji": "🚨",
            "description": "Report an issue, request a DNI, or flag safety/security concerns.",
            "verification": False,
            "open_voice": False,
            "staff_role_ids": [],
        },
        {
            "label": "PARTNERSHIP",
            "value": "partnership",
            "code": "PART",
            "parent_category_id": None,
            "emoji": "🤝",
            "description": "Request a partnership review with our team.",
            "verification": False,
            "open_voice": False,
            "staff_role_ids": [],
        },
        {
            "label": "PROMOTION",
            "value": "promotion",
            "code": "PRM",
            "parent_category_id": None,
            "emoji": "📣",
            "description": "Request promo for events, socials, streaming, artwork, or adult links.",
            "verification": False,
            "open_voice": False,
            "staff_role_ids": [],
        },
    ]

    for d in defaults:
        if d["value"].lower() not in existing:
            cfg["panel_options"].append(d)

    seen = set()
    cfg["panel_options"] = [
        o for o in cfg["panel_options"]
        if not (o.get("value", "").lower() in seen or seen.add(o.get("value", "").lower()))
    ]


# ----------------------------
# Cog
# ----------------------------
class TicketChannelsCog(commands.Cog):
    """Handles ticket panel setup, verification categories, and ticket management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.has_permissions(administrator=True)
    @commands.command(name="ticketspanel_chan")
    async def ticketspanel_chan(self, ctx: commands.Context):
        cfg = tickets_cfg(self.bot)
        opts = cfg.get("panel_options", [])
        if not opts:
            await ctx.send("No ticket panel options configured.")
            return

        lines = []
        for o in opts:
            lines.append(
                f"**{o.get('label')}** (`{o.get('value')}`) "
                f"code={o.get('code')} verification={o.get('verification')} voice={o.get('open_voice')}"
            )

        text = "\n".join(lines)
        msg, f = _as_text_or_file(text)
        await ctx.send(content=msg, file=f)


# ----------------------------
# Setup entry point
# ----------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(TicketChannelsCog(bot))
