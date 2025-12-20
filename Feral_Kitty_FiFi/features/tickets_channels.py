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
    openpyxl = None  # optional; used by !tickets report


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
    cfg.setdefault("staff_role_ids", [])  # preferred: IDs
    cfg.setdefault("roles_to_ping_names", [])  # fallback by names
    cfg.setdefault("panel", {
        "hub_channel_id": None,
        "image_url": "",
        "title": "How can we help?",
        "description": "Pick a category below to open a ticket channel.",
        "colors": ["#5865F2"],
    })
    cfg.setdefault("panel_options", [])  # up to 6 shown
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
    if "panel_options" not in cfg or not isinstance(cfg["panel_options"], list):
        cfg["panel_options"] = []

    existing = {str(o.get("value", "")).lower() for o in cfg["panel_options"]}
    defaults = [
        {"label": "ID VERIFY", "value": "id_verification", "code": "IDV", "parent_category_id": None, "emoji": "🪪",
         "description": "Upload a valid ID for age verification.", "verification": True, "open_voice": False, "staff_role_ids": []},
        {"label": "CROSS VERIFY", "value": "cross_verification", "code": "XVER", "parent_category_id": None, "emoji": "🧩",
         "description": "Request cross verification from another server.", "verification": True, "open_voice": False, "staff_role_ids": []},
        {"label": "VC VERIFY", "value": "video_verification", "code": "VVER", "parent_category_id": None, "emoji": "🎥",
         "description": "Verify your age over a quick video call.", "verification": True, "open_voice": True, "staff_role_ids": []},
        {"label": "REPORT", "value": "report", "code": "RPT", "parent_category_id": None, "emoji": "🚨",
         "description": "Report an issue, request a DNI, or flag safety/security concerns.", "verification": False, "open_voice": False, "staff_role_ids": []},
        {"label": "PARTNERSHIP", "value": "partnership", "code": "PART", "parent_category_id": None, "emoji": "🤝",
         "description": "Request a partnership review with our team.", "verification": False, "open_voice": False, "staff_role_ids": []},
        {"label": "PROMOTION", "value": "promotion", "code": "PRM", "parent_category_id": None, "emoji": "📣",
         "description": "Request promo for events, socials, streaming, artwork, or adult links.", "verification": False, "open_voice": False, "staff_role_ids": []},
    ]
    for d in defaults:
        if d["value"].lower() not in existing:
            cfg["panel_options"].append(d)

    seen = set()
    unique = []
    for o in cfg["panel_options"]:
        v = str(o.get("value", "")).lower()
        if v not in seen:
            seen.add(v)
            unique.append(o)
    cfg["panel_options"] = unique

def _option_for_value(cfg: Dict[str, Any], value: str) -> Optional[Dict[str, Any]]:
    value = str(value).lower()
    for o in (cfg.get("panel_options") or []):
        if str(o.get("value", "")).lower() == value:
            return o
    return None

def _resolve_staff_role_ids(guild: discord.Guild, cfg: Dict[str, Any]) -> List[int]:
    # Prefer explicit IDs; fallback to names
    ids = [int(x) for x in (cfg.get("staff_role_ids") or []) if isinstance(x, int) or str(x).isdigit()]
    ids = [i for i in ids if guild.get_role(i)]
    if ids:
        return sorted(set(ids))
    names = cfg.get("roles_to_ping_names") or []
    out: List[int] = []
    for name in names:
        r = resolve_role_any(guild, str(name))
        if r:
            out.append(r.id)
    return sorted(set(out))


# ----------------------------
# UI: panel + selection -> create channel
# ----------------------------
class TicketSelect(discord.ui.Select):
    def __init__(self, cog: "TicketChannelsCog", hub: discord.TextChannel):
        self.cog = cog
        self.hub = hub
        cfg = tickets_cfg(cog.bot)

        options: List[discord.SelectOption] = []
        for o in (cfg.get("panel_options") or [])[:6]:
            options.append(
                discord.SelectOption(
                    label=str(o.get("label") or o.get("value") or "Option")[:100],
                    value=str(o.get("value") or "")[:100],
                    description=(str(o.get("description") or "")[:95] or None),
                    emoji=o.get("emoji") or None,
                )
            )

        super().__init__(
            placeholder="Select a ticket type…",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not bool(options),
            custom_id="tickets:panel_select",
        )

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        if not interaction.guild or not isinstance(user, discord.Member):
            return await interaction.response.send_message("Only server members can open tickets.", ephemeral=True)

        cfg = tickets_cfg(self.cog.bot)
        value = self.values[0]
        opt = _option_for_value(cfg, value)
        if not opt:
            return await interaction.response.send_message("That ticket option is unavailable.", ephemeral=True)

        parent_id = opt.get("parent_category_id")
        parent = interaction.guild.get_channel(int(parent_id)) if parent_id else None
        if parent_id and not isinstance(parent, discord.CategoryChannel):
            return await interaction.response.send_message("Configured ticket category is invalid.", ephemeral=True)

        # resolve staff ids
        staff_ids = []
        # option-specific first
        opt_ids = [int(x) for x in (opt.get("staff_role_ids") or []) if isinstance(x, int) or str(x).isdigit()]
        opt_ids = [i for i in opt_ids if interaction.guild.get_role(i)]
        if opt_ids:
            staff_ids = sorted(set(opt_ids))
        else:
            staff_ids = _resolve_staff_role_ids(interaction.guild, cfg)

        verification = bool(opt.get("verification", False))
        open_voice = bool(opt.get("open_voice", False))

        # Name: YYYYMM-last4UID-#### (no username)
        last4 = f"{user.id % 10000:04d}"
        seq_bucket = cfg.setdefault("counters", {}).setdefault(yyyymm(), {})
        next_seq = int(seq_bucket.get(value, 1))
        seq_bucket[value] = next_seq + 1
        base_name = f"{yyyymm()}-{last4}-{next_seq:04d}"

        # perms
        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(view_channel=False, read_message_history=False)
        overwrites[user] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=True, attach_files=True, embed_links=True)
        for rid in staff_ids:
            r = interaction.guild.get_role(rid)
            if r:
                overwrites[r] = discord.PermissionOverwrite(
                    view_channel=True, read_message_history=True, send_messages=True, attach_files=True, manage_messages=True, embed_links=True
                )

        # create text
        try:
            text_ch = await interaction.guild.create_text_channel(
                name=base_name, category=parent, overwrites=overwrites or None, reason=f"Ticket opened by {user} ({value})"
            )
        except discord.Forbidden:
            return await interaction.response.send_message("I lack permission to create ticket channels.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"Failed to create ticket: {e}", ephemeral=True)

        # optional voice
        voice_ch: Optional[discord.VoiceChannel] = None
        if open_voice:
            v_overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
            v_overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(connect=False, view_channel=False)
            v_overwrites[user] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True)
            for rid in staff_ids:
                r = interaction.guild.get_role(rid)
                if r:
                    v_overwrites[r] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True)
            try:
                voice_ch = await interaction.guild.create_voice_channel(
                    name=f"{base_name}-vc"[:100], category=parent, overwrites=v_overwrites or None, reason=f"Ticket voice opened by {user} ({value})"
                )
            except Exception:
                voice_ch = None  # non-fatal

        # record
        rec = {
            "opened_at": now_iso(),
            "category": value,
            "opener_name": str(user),
            "opener_id": user.id,
            "channel_id": text_ch.id,
            "voice_channel_id": voice_ch.id if voice_ch else None,
            "claimed_by_name": "",
            "claimed_by_id": None,
            "claimed_at": "",
            "closed_at": "",
            "transcript_msg_url": "",
            "transcript_cdn_url": "",
            "archived": False,
        }
        cfg.setdefault("records", []).append(rec)
        cfg.setdefault("active", {})[str(text_ch.id)] = {
            "opener_id": user.id,
            "value": value,
            "opened_at": rec["opened_at"],
            "claimed_by": None,
            "claimed_at": None,
            "verification": verification,
            "voice_channel_id": voice_ch.id if voice_ch else None,
        }
        await save_config(self.cog.bot.config)

        # intro + ping
        mentions = []
        for rid in staff_ids:
            r = interaction.guild.get_role(rid)
            if r:
                mentions.append(r.mention)

        label = opt.get("label") or value
        intro = discord.Embed(
            title=f"Ticket: {label}",
            description=(
                f"Opened by {user.mention} • {ts_fmt(datetime.now(timezone.utc))}\n\n"
                + ("🎥 **Voice channel created:** " + (voice_ch.mention if voice_ch else "_failed to create_") + "\n\n" if open_voice else "")
                + "Provide details below."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        try:
            if mentions:
                await text_ch.send(content=safe_join_mentions(mentions), embed=intro,
                                   allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False))
            else:
                await text_ch.send(embed=intro)
        except Exception:
            pass

        await interaction.response.send_message(f"✅ Ticket created: {text_ch.mention}", ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "TicketChannelsCog", hub: discord.TextChannel):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(cog, hub))


# ----------------------------
# Cog
# ----------------------------
class TicketChannelsCog(commands.Cog):
    """Ticket panel + creation for up to 6 options."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---- post panel ----
    async def _post_panel(self, hub: discord.TextChannel):
        cfg = tickets_cfg(self.bot)
        panel = cfg.get("panel") or {}
        title = panel.get("title") or "How can we help?"
        desc = panel.get("description") or "Pick a category below to open a ticket channel."
        image_url = panel.get("image_url") or ""
        colors = panel.get("colors") or ["#5865F2"]
        color_val = parse_hex_color(colors[0] if colors else "#5865F2")

        view = TicketPanelView(self, hub)
        emb = discord.Embed(
            title=str(title)[:256],
            description=str(desc)[:4096],
            color=color_val,
            timestamp=datetime.now(timezone.utc),
        )
        if image_url:
            emb.set_image(url=image_url)

        await hub.send(embed=emb, view=view)

    # ---- admin command ----
    @commands.has_permissions(administrator=True)
    @commands.command(name="ticketspanel_chan")
    async def ticketspanel_chan(self, ctx: commands.Context, sub: str = None, *, rest: str = ""):
        """
        Ticket panel/config commands:

        - Post panel (uses config):
            !ticketspanel_chan

        - Post panel to a hub and SAVE it to config:
            !ticketspanel_chan create <#hub> [image_url]

        - List options:
            !ticketspanel_chan listopts
        """
        cfg = tickets_cfg(self.bot)

        if not sub:
            panel = cfg.get("panel") or {}
            hub_id = panel.get("hub_channel_id")
            hub = resolve_channel_any(ctx.guild, hub_id)
            if not isinstance(hub, discord.TextChannel):
                await ctx.send("❌ tickets.panel.hub_channel_id is not set to a valid text channel in config.")
                return
            await self._post_panel(hub)
            await ctx.send(f"✅ Panel posted in {hub.mention}.")
            return

        sub_l = sub.lower().strip()

        if sub_l == "create":
            parts = (rest or "").split()
            if not parts:
                await ctx.send("Usage: `!ticketspanel_chan create #channel [image_url]`")
                return
            hub = resolve_channel_any(ctx.guild, parts[0])
            if not isinstance(hub, discord.TextChannel):
                await ctx.send("❌ Provide a valid text channel for the hub.")
                return
            image_url = parts[1] if len(parts) > 1 else (cfg.get("panel", {}) or {}).get("image_url", "")

            cfg.setdefault("panel", {})["hub_channel_id"] = hub.id
            cfg.setdefault("panel", {})["image_url"] = image_url
            await save_config(self.bot.config)

            await self._post_panel(hub)
            await ctx.send(f"✅ Panel posted and saved to config in {hub.mention}.")
            return

        if sub_l == "listopts":
            opts = cfg.get("panel_options") or []
            if not opts:
                await ctx.send("ℹ️ No options configured.")
                return
            lines = []
            for o in opts:
                lines.append(
                    f"- **{o.get('label')}** (`{o.get('value')}`) "
                    f"code=`{o.get('code')}` parent=`{o.get('parent_category_id')}` "
                    f"verification={o.get('verification', False)} voice={o.get('open_voice', False)} "
                    f"roles={o.get('staff_role_ids', [])}"
                )
            text = "\n".join(lines)
            msg, f = _as_text_or_file(text)
            await ctx.send(content=msg, file=f)
            return

        await ctx.send("Usage: `!ticketspanel_chan` | `!ticketspanel_chan create #hub [image_url]` | `!ticketspanel_chan listopts`")


# ----------------------------
# Setup entry point
# ----------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(TicketChannelsCog(bot))
