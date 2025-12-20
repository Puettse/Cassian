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
    # keep well under 2000
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
    cfg.setdefault("roles_to_ping_names", [])  # fallback: resolve to IDs if staff_role_ids empty
    cfg.setdefault("panel", {
        "hub_channel_id": None,
        "image_url": "",
        "title": "How can we help?",
        "description": "Pick a category below to open a ticket channel.",
        "colors": ["#5865F2"],
    })
    cfg.setdefault("panel_options", [])  # up to 6 shown
    cfg.setdefault("delete_images", {"scan_limit": 500})
    cfg.setdefault("counters", {})  # {yyyymm: {value: next_seq}}
    cfg.setdefault("active", {})    # {channel_id: meta}
    cfg.setdefault("records", [])   # list of reports
    cfg.setdefault("allow_user_close", False)
    cfg.setdefault("archive", {"enabled": True, "category_id": None, "rename_prefix": "closed-"})
    cfg.setdefault("transcripts", {"format": "html"})
    _ensure_verification_defaults(cfg)
    return cfg

def _ensure_verification_defaults(cfg: Dict[str, Any]) -> None:
    # Inject 3 verification options if missing (value keys are canonical).
    have = {str(o.get("value", "")).lower() for o in (cfg.get("panel_options") or [])}
    defaults = [
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
    ]
    for opt in defaults:
        if opt["value"] not in have:
            cfg.setdefault("panel_options", []).insert(0, opt)

def _option_for_value(cfg: Dict[str, Any], value: str) -> Optional[Dict[str, Any]]:
    value = str(value).lower()
    for o in (cfg.get("panel_options") or []):
        if str(o.get("value", "")).lower() == value:
            return o
    return None

def _resolve_staff_role_ids(guild: discord.Guild, cfg: Dict[str, Any]) -> List[int]:
    # Prefer explicit IDs; fallback to names
    ids = [int(x) for x in (cfg.get("staff_role_ids") or []) if str(x).isdigit() or isinstance(x, int)]
    ids = [i for i in ids if guild.get_role(i)]
    if ids:
        return sorted(set(ids))

    names = cfg.get("roles_to_ping_names") or []
    out: List[int] = []
    for name in names:
        role = resolve_role_any(guild, str(name))
        if role:
            out.append(role.id)
    out = sorted(set(out))
    if out:
        cfg["staff_role_ids"] = out  # persist for next time
    return out

def _staff_role_ids_for_option(guild: discord.Guild, cfg: Dict[str, Any], value: str) -> List[int]:
    opt = _option_for_value(cfg, value) or {}
    opt_ids = [int(x) for x in (opt.get("staff_role_ids") or []) if isinstance(x, int) or str(x).isdigit()]
    opt_ids = [i for i in opt_ids if guild.get_role(i)]
    if opt_ids:
        return sorted(set(opt_ids))
    # fallback to global staff
    return _resolve_staff_role_ids(guild, cfg)

def _member_is_staff(member: discord.Member, staff_ids: List[int]) -> bool:
    return any(r.id in set(staff_ids) for r in member.roles)

def incr_counter(bot: commands.Bot, value: str) -> int:
    cfg = tickets_cfg(bot)
    bucket = cfg.setdefault("counters", {}).setdefault(yyyymm(), {})
    seq = int(bucket.get(value, 1))
    bucket[value] = seq + 1
    return seq


# ----------------------------
# Transcript exporters
# ----------------------------
async def export_channel_json(channel: discord.TextChannel) -> Tuple[str, io.BytesIO]:
    msgs = []
    async for m in channel.history(limit=None, oldest_first=True):
        msgs.append({
            "id": m.id,
            "author": {"id": m.author.id, "name": str(m.author), "bot": bool(getattr(m.author, "bot", False))},
            "created_at_iso": m.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "content": m.content,
            "attachments": [{"id": a.id, "filename": a.filename, "url": a.url, "size": a.size} for a in m.attachments],
            "embeds": [{"type": e.type, "title": getattr(e, "title", None), "description": getattr(e, "description", None)} for e in m.embeds],
            "jump_url": m.jump_url,
        })
    payload = {
        "channel": {"id": channel.id, "name": channel.name},
        "exported_at_iso": now_iso(),
        "count": len(msgs),
        "messages": msgs,
    }
    buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    buf.seek(0)
    return f"transcript_{channel.id}.json", buf

async def export_channel_html(channel: discord.TextChannel) -> Tuple[str, io.BytesIO]:
    def esc(s: str) -> str:
        return html.escape(s or "", quote=True)

    parts: List[str] = []
    parts.append("<!DOCTYPE html><meta charset='utf-8'>")
    parts.append(f"<title>Transcript — {esc(channel.name)}</title>")
    parts.append(
        "<style>"
        "body{font:14px system-ui,Segoe UI,Arial,sans-serif;background:#0b0b10;color:#e6e6e6;margin:24px}"
        ".msg{padding:10px 12px;margin:8px 0;border:1px solid #2a2a3a;border-radius:10px;background:#11121a}"
        ".meta{opacity:.75;font-size:12px;margin-bottom:6px}"
        ".author{font-weight:600}"
        ".content{white-space:pre-wrap}"
        "a{color:#8ab4ff}"
        "</style>"
    )
    parts.append(f"<h2>Transcript — #{esc(channel.name)}</h2>")
    parts.append(f"<p>Channel ID: {channel.id} • Exported: {esc(now_iso())}</p><hr>")

    async for m in channel.history(limit=None, oldest_first=True):
        author = esc(str(m.author))
        ts = m.created_at.replace(tzinfo=timezone.utc).isoformat()
        content = esc(m.content or "")
        parts.append("<div class='msg'>")
        parts.append(f"<div class='meta'><span class='author'>{author}</span> • <code>{ts}</code> • <a href='{m.jump_url}'>jump</a></div>")
        if content:
            parts.append(f"<div class='content'>{content}</div>")
        for e in m.embeds:
            title = esc(getattr(e, "title", "") or "")
            desc = esc(getattr(e, "description", "") or "")
            if title or desc:
                parts.append(f"<div class='content'><em>{title}</em><br>{desc}</div>")
        if m.attachments:
            parts.append("<div class='content'>Attachments:<ul>")
            for a in m.attachments:
                parts.append(f"<li><a href='{a.url}' target='_blank'>{esc(a.filename)}</a> ({a.size} bytes)</li>")
            parts.append("</ul></div>")
        parts.append("</div>")

    buf = io.BytesIO("\n".join(parts).encode("utf-8"))
    buf.seek(0)
    return f"transcript_{channel.id}.html", buf


# ----------------------------
# UI: panel
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
            custom_id="tickets:panel_select",  # makes it persistent-friendly
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

        staff_ids = _staff_role_ids_for_option(interaction.guild, cfg, value)
        if not staff_ids:
            # still allow creation, but warns you why no ping
            pass

        verification = bool(opt.get("verification", False))
        open_voice = bool(opt.get("open_voice", False))

        # Name: YYYYMM-last4UID-#### (no username)
        last4 = f"{user.id % 10000:04d}"
        seq = incr_counter(self.cog.bot, value)
        base_name = f"{yyyymm()}-{last4}-{seq:04d}"

        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(view_channel=False, read_message_history=False)

        # opener perms (text)
        overwrites[user] = discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=True,
            attach_files=True,
            embed_links=True,
        )

        # staff perms (text)
        for rid in staff_ids:
            r = interaction.guild.get_role(rid)
            if r:
                overwrites[r] = discord.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=True,
                    attach_files=True,
                    manage_messages=True,
                    embed_links=True,
                )

        # create ticket text channel always
        try:
            text_ch = await interaction.guild.create_text_channel(
                name=base_name,
                category=parent,
                overwrites=overwrites or None,
                reason=f"Ticket opened by {user} ({value})",
            )
        except discord.Forbidden:
            return await interaction.response.send_message("I lack permission to create ticket channels.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"Failed to create ticket: {e}", ephemeral=True)

        voice_ch: Optional[discord.VoiceChannel] = None
        if open_voice:
            # create a voice channel too, so user can connect/cam; keep same perms idea
            v_overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
            v_overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(connect=False, view_channel=False)
            v_overwrites[user] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True)
            for rid in staff_ids:
                r = interaction.guild.get_role(rid)
                if r:
                    v_overwrites[r] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True)
            try:
                voice_ch = await interaction.guild.create_voice_channel(
                    name=f"{base_name}-vc"[:100],
                    category=parent,
                    overwrites=v_overwrites or None,
                    reason=f"Ticket voice opened by {user} ({value})",
                )
            except Exception:
                voice_ch = None  # non-fatal

        # Record
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

        # Ping staff roles (if any)
        mentions = []
        for rid in staff_ids:
            r = interaction.guild.get_role(rid)
            if r:
                mentions.append(r.mention)

        # Intro embed
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

        ping_text = safe_join_mentions(mentions)
        try:
            if ping_text:
                await text_ch.send(content=ping_text, embed=intro, allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False))
            else:
                await text_ch.send(embed=intro)
        except Exception:
            pass

        await self.cog._post_controls(text_ch, opener=user, opt_value=value, verification=verification)

        await interaction.response.send_message(f"✅ Ticket created: {text_ch.mention}", ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "TicketChannelsCog", hub: discord.TextChannel):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(cog, hub))


# ----------------------------
# UI: controls (Claim / Delete Images / Close)
# ----------------------------
class CloseModal(discord.ui.Modal, title="Close Ticket — Resolution"):
    def __init__(self, cog: "TicketChannelsCog", channel: discord.TextChannel, opener_id: int, staff_ids: List[int]):
        super().__init__(timeout=180)
        self.cog = cog
        self.channel = channel
        self.opener_id = opener_id
        self.staff_ids = staff_ids
        self.resolution = discord.ui.TextInput(
            label="Resolution / Notes",
            style=discord.TextStyle.long,
            placeholder="What was done / final notes…",
            max_length=1000,
            required=False,
        )
        self.add_item(self.resolution)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._handle_close(
            interaction,
            self.channel,
            self.opener_id,
            self.staff_ids,
            str(self.resolution.value or "").strip(),
        )


class TicketControlsView(discord.ui.View):
    def __init__(self, cog: "TicketChannelsCog", channel: discord.TextChannel, opener_id: int, value: str, verification: bool):
        super().__init__(timeout=None)
        self.cog = cog
        self.channel = channel
        self.opener_id = opener_id
        self.value = value
        self.verification = verification

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, emoji="🧰", custom_id="tickets:claim")
    async def claim_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        cfg = tickets_cfg(self.cog.bot)
        staff_ids = _staff_role_ids_for_option(interaction.guild, cfg, self.value)

        if not isinstance(interaction.user, discord.Member) or not _member_is_staff(interaction.user, staff_ids):
            return await interaction.response.send_message("You must be staff to claim.", ephemeral=True)

        meta = cfg.get("active", {}).get(str(self.channel.id))
        if not meta:
            return await interaction.response.send_message("This channel is not tracked as a ticket.", ephemeral=True)
        if meta.get("claimed_by"):
            return await interaction.response.send_message("Already claimed.", ephemeral=True)

        meta["claimed_by"] = interaction.user.id
        meta["claimed_at"] = now_iso()

        for rec in reversed(cfg.get("records", [])):
            if rec.get("channel_id") == self.channel.id:
                rec["claimed_by_name"] = str(interaction.user)
                rec["claimed_by_id"] = interaction.user.id
                rec["claimed_at"] = now_iso()
                break

        await save_config(self.cog.bot.config)
        await self.cog._refresh_controls_embed(self.channel, opener_id=self.opener_id, value=self.value, verification=self.verification)

        # log claim
        log_ch = resolve_channel_any(interaction.guild, cfg.get("log_channel_id"))
        if isinstance(log_ch, discord.TextChannel):
            em = discord.Embed(
                title="Ticket Claimed",
                description=f"Channel: {self.channel.mention}\nBy: {interaction.user.mention}\nWhen: {ts_fmt(datetime.now(timezone.utc))}",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            try:
                await log_ch.send(embed=em)
            except Exception:
                pass

        await interaction.response.send_message("✅ Claimed.", ephemeral=True)

    @discord.ui.button(label="Delete Image Messages", style=discord.ButtonStyle.secondary, emoji="🗑️", custom_id="tickets:delete_images")
    async def delete_images_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.verification:
            return await interaction.response.send_message("Not available for this ticket.", ephemeral=True)

        cfg = tickets_cfg(self.cog.bot)
        scan_limit = int((cfg.get("delete_images") or {}).get("scan_limit", 500))
        scan_limit = max(50, min(2000, scan_limit))

        deleted, skipped = 0, 0
        try:
            async for m in self.channel.history(limit=scan_limit, oldest_first=False):
                has_image = False
                for a in m.attachments:
                    ctype = (a.content_type or "").lower()
                    if ctype.startswith("image/") or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                        has_image = True
                        break
                if has_image:
                    try:
                        await m.delete(reason=f"Delete Images pressed by {interaction.user} in ticket {self.channel.id}")
                        deleted += 1
                    except Exception:
                        skipped += 1

            # visible log in-ticket
            try:
                await self.channel.send(f"🗑️ **Image message purge** pressed by {interaction.user.mention}. Deleted **{deleted}**, skipped **{skipped}**.")
            except Exception:
                pass

            await interaction.response.send_message(f"🗑️ Deleted {deleted} image messages. Skipped {skipped}.", ephemeral=True)

        except Exception:
            await interaction.response.send_message("Failed to scan/delete image messages (permissions/rate limit).", ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="tickets:close")
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        cfg = tickets_cfg(self.cog.bot)
        staff_ids = _staff_role_ids_for_option(interaction.guild, cfg, self.value)

        allowed = False
        if isinstance(interaction.user, discord.Member):
            if _member_is_staff(interaction.user, staff_ids):
                allowed = True
            elif cfg.get("allow_user_close") and interaction.user.id == self.opener_id:
                allowed = True

        if not allowed:
            return await interaction.response.send_message("You are not allowed to close this ticket.", ephemeral=True)

        await interaction.response.send_modal(CloseModal(self.cog, self.channel, self.opener_id, staff_ids))


# ----------------------------
# Cog
# ----------------------------
class TicketChannelsCog(commands.Cog):
    """
    Ticket System:
      - Panel dropdown (up to 6 options)
      - Creates ticket text channel named: YYYYMM-last4UID-#### (no username)
      - Video verification also creates a voice channel: YYYYMM-last4UID-####-vc
      - Buttons: Claim, Close, Delete Image Messages (verification-only)
      - HTML (or JSON) transcripts attached to log channel and DM to opener
      - XLSX report: !tickets report
      - Autoconfig triggers: $CONFIGTICKET$ and $CONFIGTICKETCUSTOM$
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ----------------------------
    # Autoconfig triggers (message-based, like your $help$)
    # ----------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        content = (message.content or "").strip().upper()
        if content == "$CONFIGTICKET$":
            # Build from config only (post panel to hub, resolve staff ids, ensure defaults)
            if not isinstance(message.author, discord.Member) or not message.author.guild_permissions.administrator:
                return
            await self._autoconfig_from_config(message.channel, custom=False)
        elif content == "$CONFIGTICKETCUSTOM$":
            if not isinstance(message.author, discord.Member) or not message.author.guild_permissions.administrator:
                return
            await self._autoconfig_from_config(message.channel, custom=True)

    async def _autoconfig_from_config(self, reply_channel: discord.abc.Messageable, custom: bool):
        cfg = tickets_cfg(self.bot)

        guild = None
        if isinstance(reply_channel, discord.TextChannel):
            guild = reply_channel.guild
        if not guild:
            return

        # If custom, attempt to create categories/channels and write ids into config.
        if custom:
            # Create categories/channels if missing — simple, safe naming.
            # You can rename later; IDs are what matter.
            async def ensure_category(name: str) -> discord.CategoryChannel:
                existing = discord.utils.get(guild.categories, name=name)
                if existing:
                    return existing
                return await guild.create_category(name=name, reason="Ticket custom autoconfig")

            async def ensure_text_in_category(cat: discord.CategoryChannel, name: str) -> discord.TextChannel:
                existing = discord.utils.get(cat.text_channels, name=name)
                if existing:
                    return existing
                return await guild.create_text_channel(name=name, category=cat, reason="Ticket custom autoconfig")

            try:
                archive_cat = await ensure_category("MUFFIN ARCHIVE")
                tickets_cat = await ensure_category("MUFFIN TICKETS")
                logs_cat = await ensure_category("MUFFIN LOGS")
                control_cat = await ensure_category("MUFFIN CONTROL")

                logs_ch = await ensure_text_in_category(logs_cat, "tickets-log")
                hub_ch = await ensure_text_in_category(control_cat, "tickets-panel")

                cfg.setdefault("archive", {})["enabled"] = True
                cfg["archive"]["category_id"] = archive_cat.id
                cfg.setdefault("panel", {})["hub_channel_id"] = hub_ch.id
                cfg["log_channel_id"] = logs_ch.id

                # If options missing categories, default them to tickets_cat
                for o in (cfg.get("panel_options") or []):
                    if not o.get("parent_category_id"):
                        o["parent_category_id"] = tickets_cat.id

                await save_config(self.bot.config)
            except Exception:
                pass

        # Resolve staff ids from names if needed and persist
        staff_ids = _resolve_staff_role_ids(guild, cfg)
        if staff_ids:
            await save_config(self.bot.config)

        # Post panel to hub
        panel = cfg.get("panel") or {}
        hub_id = panel.get("hub_channel_id")
        hub = resolve_channel_any(guild, hub_id)
        if not isinstance(hub, discord.TextChannel):
            await reply_channel.send("❌ tickets.panel.hub_channel_id is not set to a valid text channel. Fix config and try again.")
            return

        await self._post_panel(hub)
        await reply_channel.send(f"✅ Ticket panel posted in {hub.mention} ({'CUSTOM' if custom else 'CONFIG'}).")

    # ----------------------------
    # Admin command surface
    # ----------------------------
    @commands.has_permissions(administrator=True)
    @commands.command(name="ticketspanel_chan")
    async def ticketspanel_chan(self, ctx: commands.Context, sub: str = None, *, rest: str = ""):
        """
        Ticket panel/config commands:

        - Post panel (uses config):
            !ticketspanel_chan

        - Post panel to a hub and SAVE it to config:
            !ticketspanel_chan create <#hub> [image_url]

        - Set global staff ping roles (saves IDs):
            !ticketspanel_chan staff <@Role ...>

        - Add an option (non-verification):
            !ticketspanel_chan addopt Label|value|CODE|<category_id>|emoji|Description

        - Set staff roles for one option:
            !ticketspanel_chan setoptroles <value> <@Role ...>

        - List options:
            !ticketspanel_chan listopts

        - Set archive category:
            !ticketspanel_chan setarchive <#category|id|none>

        - Transcript format:
            !ticketspanel_chan setformat <html|json>

        - XLSX report:
            !tickets report
        """
        cfg = tickets_cfg(self.bot)

        if not sub:
            # Post from config (the thing you expected to "just work")
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
            # Expect: rest starts with channel mention/id then optional image url
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

        if sub_l == "staff":
            ids: List[int] = []
            for tok in (rest or "").replace(",", " ").split():
                r = resolve_role_any(ctx.guild, tok)
                if r:
                    ids.append(r.id)
            cfg["staff_role_ids"] = sorted(set(ids))
            await save_config(self.bot.config)
            await ctx.send(f"✅ Global staff_role_ids set to: {cfg['staff_role_ids']}")
            return

        if sub_l == "addopt":
            if not rest:
                await ctx.send("Format: `Label|value|CODE|123456789012345678|emoji|Short description`")
                return
            parts = [p.strip() for p in rest.split("|")]
            if len(parts) < 6:
                await ctx.send("❌ Need 6 fields separated by `|`.")
                return
            label, value, code, parent, emoji, desc = parts[:6]
            try:
                parent_id = int(parent)
            except ValueError:
                await ctx.send("❌ parent_category_id must be a numeric ID.")
                return
            cfg.setdefault("panel_options", []).append({
                "label": label,
                "value": value,
                "code": code,
                "parent_category_id": parent_id,
                "emoji": emoji,
                "description": desc,
                "verification": False,
                "open_voice": False,
                "staff_role_ids": [],
            })
            await save_config(self.bot.config)
            await ctx.send(f"✅ Added option `{label}` → `{value}`")
            return

        if sub_l == "setoptroles":
            pieces = (rest or "").split()
            if len(pieces) < 2:
                await ctx.send("Usage: `!ticketspanel_chan setoptroles <value> <@Role ...>`")
                return
            value = pieces[0]
            opt = _option_for_value(cfg, value)
            if not opt:
                await ctx.send("❌ Option not found.")
                return
            ids: List[int] = []
            for tok in pieces[1:]:
                r = resolve_role_any(ctx.guild, tok)
                if r:
                    ids.append(r.id)
            opt["staff_role_ids"] = sorted(set(ids))
            await save_config(self.bot.config)
            await ctx.send(f"✅ Staff roles for `{value}` set to: {opt['staff_role_ids']}")
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

        if sub_l == "setarchive":
            arg = rest.strip()
            if not arg:
                await ctx.send("Usage: `!ticketspanel_chan setarchive <#category|id|none>`")
                return
            if arg.lower() == "none":
                cfg["archive"]["enabled"] = False
                cfg["archive"]["category_id"] = None
            else:
                cat = resolve_channel_any(ctx.guild, arg)
                if not isinstance(cat, discord.CategoryChannel):
                    await ctx.send("❌ Provide a category.")
                    return
                cfg["archive"]["enabled"] = True
                cfg["archive"]["category_id"] = cat.id
            await save_config(self.bot.config)
            await ctx.send(f"✅ Archive set to: {cfg['archive']}")
            return

        if sub_l == "setformat":
            fmt = rest.strip().lower()
            if fmt not in {"html", "json"}:
                await ctx.send("Use `html` or `json`.")
                return
            cfg.setdefault("transcripts", {})["format"] = fmt
            await save_config(self.bot.config)
            await ctx.send(f"✅ Transcript format set to `{fmt}`")
            return

        # short help (never > 2000)
        emb = discord.Embed(title="Ticket Panel Commands", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        emb.description = (
            "`!ticketspanel_chan` (posts panel from config)\n"
            "`!ticketspanel_chan create #hub [image_url]`\n"
            "`!ticketspanel_chan staff @Staff @SECURITY`\n"
            "`!ticketspanel_chan addopt Label|value|CODE|<category_id>|emoji|Description`\n"
            "`!ticketspanel_chan setoptroles <value> @Role...`\n"
            "`!ticketspanel_chan listopts`\n"
            "`!ticketspanel_chan setarchive <#category|id|none>`\n"
            "`!ticketspanel_chan setformat <html|json>`\n"
            "`!tickets report`"
        )
        await ctx.send(embed=emb)

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

    async def _post_controls(self, ch: discord.TextChannel, opener: discord.Member, opt_value: str, verification: bool):
        await self._refresh_controls_embed(ch, opener_id=opener.id, value=opt_value, verification=verification)

    async def _refresh_controls_embed(self, ch: discord.TextChannel, *, opener_id: int, value: str, verification: bool):
        cfg = tickets_cfg(self.bot)
        meta = cfg.get("active", {}).get(str(ch.id), {})
        claimed_by = meta.get("claimed_by")
        claimed_tag = f"<@{claimed_by}>" if claimed_by else "_unclaimed_"
        vc_id = meta.get("voice_channel_id")
        vc_line = f"\n**VC:** <#{vc_id}>" if vc_id else ""

        info = discord.Embed(
            title="Ticket Controls",
            description=(
                f"**Opener:** <@{opener_id}>\n"
                f"**Status:** {'Claimed by ' + claimed_tag if claimed_by else 'Unclaimed'}\n"
                f"**Opened:** {meta.get('opened_at', '—')}"
                f"{vc_line}"
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        view = TicketControlsView(self, ch, opener_id, value, verification)

        # Edit last “Ticket Controls” message or send new
        found: Optional[discord.Message] = None
        async for m in ch.history(limit=20, oldest_first=False):
            if m.author.id == (self.bot.user.id if self.bot.user else 0):
                for e in m.embeds:
                    if (e.title or "").lower() == "ticket controls":
                        found = m
                        break
            if found:
                break
        try:
            if found:
                await found.edit(embed=info, view=view)
            else:
                await ch.send(embed=info, view=view)
        except Exception:
            pass

    async def _handle_close(self, interaction: discord.Interaction, channel: discord.TextChannel, opener_id: int, staff_ids: List[int], resolution: str):
        cfg = tickets_cfg(self.bot)
        fmt = (cfg.get("transcripts") or {}).get("format", "html").lower()

        # Transcript
        if fmt == "json":
            fname, blob = await export_channel_json(channel)
        else:
            fname, blob = await export_channel_html(channel)

        transcript_msg_url = ""
        transcript_cdn_url = ""

        # log channel
        log_ch = resolve_channel_any(channel.guild, cfg.get("log_channel_id"))
        if isinstance(log_ch, discord.TextChannel):
            meta = cfg.get("active", {}).get(str(channel.id)) or {}
            claimed_by = meta.get("claimed_by")
            claimed_line = (f"<@{claimed_by}>" if claimed_by else "—")

            em = discord.Embed(
                title="Ticket Closed",
                description=(
                    f"Channel: {channel.mention}\n"
                    f"Opener: <@{opener_id}>\n"
                    f"Claimed by: {claimed_line}\n"
                    f"Closed by: {interaction.user.mention}\n"
                    f"When: {ts_fmt(datetime.now(timezone.utc))}\n"
                    f"Resolution: {resolution or '—'}"
                ),
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            try:
                blob.seek(0)
                msg = await log_ch.send(content="📦 Transcript attached.", embed=em, file=discord.File(blob, filename=fname))
                transcript_msg_url = msg.jump_url
                if msg.attachments:
                    transcript_cdn_url = msg.attachments[0].url
            except Exception:
                pass

        # DM opener
        opener = channel.guild.get_member(opener_id)
        if opener:
            try:
                if transcript_cdn_url:
                    blob.seek(0)
                    await opener.send(
                        content=f"Your ticket `{channel.name}` has been closed.\nResolution: {resolution or '—'}\nTranscript: {transcript_cdn_url}"
                    )
                else:
                    blob.seek(0)
                    await opener.send(
                        content=f"Your ticket `{channel.name}` has been closed.\nResolution: {resolution or '—'}",
                        file=discord.File(blob, filename=fname),
                    )
            except Exception:
                pass

        # Remove controls view
        try:
            async for m in channel.history(limit=20, oldest_first=False):
                if m.author.id == (self.bot.user.id if self.bot.user else 0):
                    for e in m.embeds:
                        if (e.title or "").lower() == "ticket controls":
                            try:
                                await m.edit(view=None)
                            except Exception:
                                pass
                            return  # end loop cleanly
        except Exception:
            pass

        # Archive or delete
        archive = cfg.get("archive") or {}
        archived = False
        try:
            if archive.get("enabled") and archive.get("category_id"):
                cat = resolve_channel_any(channel.guild, archive.get("category_id"))
                if isinstance(cat, discord.CategoryChannel):
                    new_name = f"{(archive.get('rename_prefix') or 'closed-')}{channel.name}"[:100]
                    await channel.edit(category=cat, name=new_name, reason="Ticket archived")
                    # lock everyone + opener
                    overwrites = channel.overwrites
                    everyone = channel.guild.default_role
                    pe = overwrites.get(everyone, discord.PermissionOverwrite())
                    pe.send_messages = False
                    overwrites[everyone] = pe
                    if opener:
                        po = overwrites.get(opener, discord.PermissionOverwrite())
                        po.send_messages = False
                        overwrites[opener] = po
                    await channel.edit(overwrites=overwrites, reason="Ticket closed — locked")
                    archived = True
        except Exception:
            archived = False

        try:
            if archived:
                await interaction.response.send_message("📦 Ticket archived and locked.", ephemeral=True)
            else:
                await interaction.response.send_message("🔒 Closing ticket…", ephemeral=True)
                await channel.delete(reason=f"Ticket closed by {interaction.user} — {resolution or '—'}")
        except Exception:
            pass

        # Record update
        for rec in reversed(cfg.get("records", [])):
            if rec.get("channel_id") == channel.id:
                rec["closed_at"] = now_iso()
                rec["transcript_msg_url"] = transcript_msg_url
                rec["transcript_cdn_url"] = transcript_cdn_url
                rec["archived"] = archived
                break

        cfg.get("active", {}).pop(str(channel.id), None)

        import asyncio
        if asyncio.iscoroutinefunction(save_config):
            await save_config(self.bot.config)
        else:
            await asyncio.to_thread(save_config, self.bot.config)


    # ----------------------------
    # XLSX report
    # ----------------------------
    @commands.has_permissions(administrator=True)
    @commands.command(name="tickets")
    async def tickets_cmd(self, ctx: commands.Context, sub: str = None):
        if sub != "report":
            await ctx.send("Usage: `!tickets report` — export XLSX.")
            return
        if openpyxl is None:
            await ctx.send("`openpyxl` not installed. Add it to requirements.txt.")
            return

        cfg = tickets_cfg(self.bot)
        rows = cfg.get("records", [])

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Tickets"

        hdr = [
            "opened_at (UTC yyyy-mm-dd hh:mm)",
            "category",
            "opener_name",
            "opener_id",
            "channel_id",
            "voice_channel_id",
            "transcript_msg_url",
            "transcript_cdn_url",
            "archived",
            "claimed_by_name",
            "claimed_by_id",
            "closed_at (UTC yyyy-mm-dd hh:mm)",
        ]
        ws.append(hdr)

        def fmt_iso(s: str) -> str:
            if not s:
                return ""
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                return ts_fmt(dt)
            except Exception:
                return s

        for r in rows:
            ws.append([
                fmt_iso(r.get("opened_at", "")),
                r.get("category", ""),
                r.get("opener_name", ""),
                r.get("opener_id", ""),
                r.get("channel_id", ""),
                r.get("voice_channel_id", ""),
                r.get("transcript_msg_url", ""),
                r.get("transcript_cdn_url", ""),
                "TRUE" if r.get("archived") else "FALSE",
                r.get("claimed_by_name", ""),
                r.get("claimed_by_id", ""),
                fmt_iso(r.get("closed_at", "")),
            ])

        # formatting
        for cell in ws[1]:
            cell.font = Font(bold=True)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:L{ws.max_row}"
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = 26

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        await ctx.send(file=discord.File(buf, filename=f"tickets_report_{ctx.guild.id}_{yyyymm()}.xlsx"))


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketChannelsCog(bot))
