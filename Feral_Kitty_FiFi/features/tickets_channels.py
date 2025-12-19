# file: Feral_Kitty_FiFi/features/tickets_channels.py
from __future__ import annotations

import io
import json
import html
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any
from ..config import save_config

try:
    import openpyxl
    from openpyxl.styles import Alignment, Font
except Exception:
    openpyxl = None  # optional; used by !tickets report

# ---------- small utils ----------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def ts_fmt_iso(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M")

def yyyymm(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return f"{dt.year:04d}{dt.month:02d}"

def tickets_cfg(bot: commands.Bot) -> Dict[str, Any]:
    cfg = bot.config.setdefault("tickets", {})
    cfg.setdefault("log_channel_id", None)
    cfg.setdefault("staff_role_ids", [])
    cfg.setdefault("roles_to_ping_ids", [])
    cfg.setdefault("panel", {"hub_channel_id": None, "image_url": ""})
    cfg.setdefault("panel_options", [])
    cfg.setdefault("counters", {})
    cfg.setdefault("active", {})
    cfg.setdefault("allow_user_close", True)
    cfg.setdefault("archive", {"enabled": False, "category_id": None, "rename_prefix": "closed-"})
    cfg.setdefault("transcripts", {"format": "html"})
    cfg.setdefault("delete_images", {"scan_limit": 500})
    cfg.setdefault("records", [])
    _ensure_verification_defaults(cfg)
    return cfg

def _ensure_verification_defaults(cfg: Dict[str, Any]) -> None:
    have = {str(o.get("value")).lower() for o in cfg.get("panel_options") or []}
    defaults = [
        {
            "label": "ID VERIFY",
            "value": "id_verification",
            "code": "IDV",
            "parent_category_id": None,
            "emoji": "🪪",
            "description": "Upload a valid ID for age verification.",
            "staff_role_ids": [],
            "roles_to_ping_ids": [],
            "verification": True,
            "open_voice": False,
        },
        {
            "label": "CROSS VERIFY",
            "value": "cross_verification",
            "code": "XVER",
            "parent_category_id": None,
            "emoji": "🧩",
            "description": "Request cross verification from another server.",
            "staff_role_ids": [],
            "roles_to_ping_ids": [],
            "verification": True,
            "open_voice": False,
        },
        {
            "label": "VC VERIFY",
            "value": "video_verification",
            "code": "VVER",
            "parent_category_id": None,
            "emoji": "🎥",
            "description": "Verify your age over a quick video call.",
            "staff_role_ids": [],
            "roles_to_ping_ids": [],
            "verification": True,
            "open_voice": True,
        },
    ]
    for opt in defaults:
        if opt["value"] not in have:
            cfg.setdefault("panel_options", []).append(opt)

def _option_for_value(cfg: Dict[str, Any], value: str) -> Optional[Dict[str, Any]]:
    return next((o for o in cfg.get("panel_options") or [] if str(o.get("value")).lower() == str(value).lower()), None)

def _staff_role_ids_for_option(cfg: Dict[str, Any], value: str) -> List[int]:
    opt = _option_for_value(cfg, value) or {}
    ids = (opt.get("staff_role_ids") or []) or (cfg.get("staff_role_ids") or [])
    return [int(x) for x in ids]

def _ping_role_ids_for_option(cfg: Dict[str, Any], value: str) -> List[int]:
    opt = _option_for_value(cfg, value) or {}
    global_ping = [int(x) for x in (cfg.get("roles_to_ping_ids") or [])]
    per_opt_ping = [int(x) for x in (opt.get("roles_to_ping_ids") or [])]
    staff_ids = _staff_role_ids_for_option(cfg, value)
    return sorted(set(global_ping + per_opt_ping + staff_ids))

def _member_is_staff(member: discord.Member, staff_ids: List[int]) -> bool:
    return any(r.id in staff_ids for r in member.roles)

def incr_counter(bot: commands.Bot, value: str) -> int:
    cfg = tickets_cfg(bot)
    bucket = cfg.setdefault("counters", {}).setdefault(yyyymm(), {})
    seq = int(bucket.get(value, 1))
    bucket[value] = seq + 1
    return seq

async def _find_or_create_category(guild: discord.Guild, name: str) -> discord.CategoryChannel:
    for c in guild.categories:
        if c.name == name:
            return c
    return await guild.create_category(name=name, reason="Tickets bootstrap")

async def _find_or_create_text(guild: discord.Guild, category: discord.CategoryChannel, name: str) -> discord.TextChannel:
    for ch in category.text_channels:
        if ch.name == name:
            return ch
    return await guild.create_text_channel(name=name, category=category, reason="Tickets bootstrap")

def _hex_to_color(hex_str: str, fallback: discord.Color = discord.Color.blurple()) -> discord.Color:
    try:
        s = (hex_str or "").strip().lstrip("#")
        return discord.Color(int(s, 16))
    except Exception:
        return fallback

# ---------- transcript exporters ----------
async def export_channel_json(channel: discord.TextChannel) -> Tuple[str, io.BytesIO]:
    msgs = []
    async for m in channel.history(limit=None, oldest_first=True):
        msgs.append(
            {
                "id": m.id,
                "author": {"id": m.author.id, "name": str(m.author), "bot": bool(getattr(m.author, "bot", False))},
                "created_at_iso": m.created_at.replace(tzinfo=timezone.utc).isoformat(),
                "content": m.content,
                "attachments": [
                    {"id": a.id, "filename": a.filename, "url": a.url, "size": a.size} for a in m.attachments
                ],
                "embeds": [
                    {"type": e.type, "title": getattr(e, "title", None), "description": getattr(e, "description", None)}
                    for e in m.embeds
                ],
                "jump_url": m.jump_url,
            }
        )
    buf = io.BytesIO(
        json.dumps(
            {
                "channel": {"id": channel.id, "name": channel.name},
                "exported_at_iso": now_iso(),
                "count": len(msgs),
                "messages": msgs,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
    )
    buf.seek(0)
    return f"transcript_{channel.id}.json", buf

async def export_channel_html(channel: discord.abc.GuildChannel) -> Tuple[str, io.BytesIO]:
    def esc(s: str) -> str:
        return html.escape(s or "", quote=True)

    parts: List[str] = []
    parts.append("<!DOCTYPE html><meta charset='utf-8'>")
    parts.append(f"<title>Transcript — {esc(channel.name)}</title>")
    parts.append(
        "<style>body{font:14px system-ui,Segoe UI,Arial,sans-serif;background:#0b0b10;color:#e6e6e6;margin:24px} "
        ".msg{padding:10px 12px;margin:8px 0;border:1px solid #2a2a3a;border-radius:10px;background:#11121a} "
        ".meta{opacity:.75;font-size:12px;margin-bottom:6px} .author{font-weight:600} .content{white-space:pre-wrap} "
        "a{color:#8ab4ff}</style>"
    )
    parts.append(f"<h2>Transcript — #{esc(channel.name)}</h2>")
    parts.append(f"<p>Channel ID: {channel.id} • Exported: {esc(now_iso())}</p><hr>")
    if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
        async for m in channel.history(limit=None, oldest_first=True):
            author = esc(str(m.author))
            ts = m.created_at.replace(tzinfo=timezone.utc).isoformat()
            content = esc(m.content or "")
            parts.append("<div class='msg'>")
            parts.append(
                f"<div class='meta'><span class='author'>{author}</span> • <code>{ts}</code> • <a href='{m.jump_url}'>jump</a></div>"
            )
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

# ---------- UI: panel ----------
class TicketSelect(discord.ui.Select):
    def __init__(self, cog: "TicketChannelsCog", hub: discord.TextChannel):
        self.cog = cog
        self.hub = hub
        cfg = tickets_cfg(cog.bot)
        opts = []
        for o in (cfg.get("panel_options") or [])[:6]:
            opts.append(
                discord.SelectOption(
                    label=o.get("label") or o.get("value"),
                    value=o.get("value"),
                    description=(o.get("description") or "")[:95] or None,
                    emoji=o.get("emoji") or None,
                )
            )
        super().__init__(
            placeholder=("Make a selection" if opts else "No options configured"),
            min_values=1,
            max_values=1,
            options=opts,
            disabled=not bool(opts),
        )

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        if not isinstance(user, discord.Member):
            await interaction.response.send_message("Only server members can open tickets.", ephemeral=True)
            return

        cfg = tickets_cfg(self.cog.bot)
        value = self.values[0]
        opt = _option_for_value(cfg, value)
        if not opt:
            await interaction.response.send_message("This ticket option is not available.", ephemeral=True)
            return

        parent_id = opt.get("parent_category_id")
        parent = interaction.guild.get_channel(parent_id) if parent_id else None
        if parent_id and not isinstance(parent, discord.CategoryChannel):
            await interaction.response.send_message("Configured parent category is invalid.", ephemeral=True)
            return

        last4 = f"{user.id % 10000:04d}"
        seq = incr_counter(self.cog.bot, value)
        name = f"{yyyymm()}-{last4}-{seq:04d}"

        staff_ids = _staff_role_ids_for_option(cfg, value)
        ping_ids = _ping_role_ids_for_option(cfg, value)
        verification = bool(opt.get("verification"))
        open_voice = bool(opt.get("open_voice"))

        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(
            view_channel=False, read_message_history=False
        )
        opener_po = discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=True, attach_files=True
        )
        if open_voice:
            opener_po.connect = True
            opener_po.speak = True
            opener_po.stream = True
            opener_po.use_voice_activation = True
        overwrites[user] = opener_po
        for rid in staff_ids:
            r = interaction.guild.get_role(rid)
            if r:
                po = discord.PermissionOverwrite(
                    view_channel=True, read_message_history=True, send_messages=True, attach_files=True
                )
                if open_voice:
                    po.connect = True
                    po.speak = True
                    po.stream = True
                    po.use_voice_activation = True
                overwrites[r] = po

        try:
            if open_voice:
                ch = await interaction.guild.create_voice_channel(
                    name=name, category=parent, overwrites=overwrites or None, reason=f"Ticket (voice) opened by {user} ({value})"
                )
            else:
                ch = await interaction.guild.create_text_channel(
                    name=name, category=parent, overwrites=overwrites or None, reason=f"Ticket opened by {user} ({value})"
                )
        except discord.Forbidden:
            await interaction.response.send_message("I lack permission to create the ticket channel here.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Failed to create ticket: {e}", ephemeral=True)
            return

        rec = {
            "opened_at": now_iso(),
            "option": value,
            "opener_name": str(user),
            "opener_id": user.id,
            "channel_id": ch.id,
            "claimed_by_name": "",
            "claimed_by_id": None,
            "claimed_at": "",
            "closed_at": "",
            "transcript_msg_url": "",
            "transcript_cdn_url": "",
            "archived": False,
        }
        cfg.setdefault("records", []).append(rec)
        cfg.setdefault("active", {})[str(ch.id)] = {
            "opener_id": user.id,
            "value": value,
            "opened_at": rec["opened_at"],
            "claimed_by": None,
            "claimed_at": None,
            "verification": verification,
            "open_voice": open_voice,
        }
        await save_config(self.cog.bot.config)

        mentions: List[str] = []
        for rid in ping_ids:
            r = interaction.guild.get_role(int(rid))
            if r:
                mentions.append(r.mention)

        intro = discord.Embed(
            title=f"Ticket: {opt.get('label') or value}",
            description=f"Opened by {user.mention} • {ts_fmt_iso(datetime.now(timezone.utc))}\n\nProvide details below.",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        if isinstance(ch, discord.TextChannel):
            if mentions:
                await ch.send(
                    content=" ".join(mentions),
                    embed=intro,
                    allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
                )
            else:
                await ch.send(embed=intro)
        else:
            try:
                if mentions:
                    await ch.send(
                        content=" ".join(mentions),
                        embed=intro,
                        allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
                    )
                else:
                    await ch.send(embed=intro)
            except Exception:
                pass

        await self.cog._post_controls(ch, opener=user, opt_value=value, verification=verification)
        await interaction.response.send_message(f"✅ Ticket created: {ch.mention}", ephemeral=True)

class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "TicketChannelsCog", hub: discord.TextChannel):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(cog, hub))

# ---------- UI: controls ----------
class CloseModal(discord.ui.Modal, title="Close Ticket — Resolution"):
    def __init__(self, cog: "TicketChannelsCog", channel: discord.abc.GuildChannel, opener_id: int, staff_ids: List[int]):
        super().__init__(timeout=120)
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
            interaction, self.channel, self.opener_id, self.staff_ids, str(self.resolution.value or "").strip()
        )

class TicketControlsView(discord.ui.View):
    def __init__(self, cog: "TicketChannelsCog", channel: discord.abc.GuildChannel, opener_id: int, value: str, verification: bool):
        super().__init__(timeout=None)
        self.cog = cog
        self.channel = channel
        self.opener_id = opener_id
        self.value = value
        self.verification = verification
        if not self.verification:
            for item in list(self.children):
                if isinstance(item, discord.ui.Button) and getattr(item.callback, "__name__", "") == "delete_images_btn":
                    self.remove_item(item)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, emoji="🧰")
    async def claim_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        cfg = tickets_cfg(self.cog.bot)
        staff_ids = _staff_role_ids_for_option(cfg, self.value)
        if not isinstance(interaction.user, discord.Member) or not _member_is_staff(interaction.user, staff_ids):
            await interaction.response.send_message("You must be staff to claim.", ephemeral=True)
            return
        meta = cfg.get("active", {}).get(str(self.channel.id))
        if not meta:
            await interaction.response.send_message("This channel is not tracked as a ticket.", ephemeral=True)
            return
        if meta.get("claimed_by"):
            await interaction.response.send_message("Already claimed.", ephemeral=True)
            return
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
        log_ch = resolve_channel_any(self.channel.guild, cfg.get("log_channel_id"))
        if isinstance(log_ch, discord.TextChannel):
            em = discord.Embed(
                title="Ticket Claimed",
                description=f"Channel: {self.channel.mention}\nBy: {interaction.user.mention}\nWhen: {ts_fmt_iso(datetime.now(timezone.utc))}",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            try:
                await log_ch.send(embed=em)
            except Exception:
                pass
        await interaction.response.send_message("✅ Claimed.", ephemeral=True)

    @discord.ui.button(label="Delete Images", style=discord.ButtonStyle.secondary, emoji="🗑️")
    async def delete_images_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.verification:
            await interaction.response.send_message("Not available for this ticket.", ephemeral=True)
            return
        cfg = tickets_cfg(self.cog.bot)
        scan_limit_cfg = int(((cfg.get("delete_images") or {}).get("scan_limit")) or 500)
        limit = scan_limit_cfg if isinstance(self.channel, discord.TextChannel) else min(scan_limit_cfg, 200)
        deleted, skipped = 0, 0
        try:
            async for m in self.channel.history(limit=limit, oldest_first=False):
                has_image = False
                for a in m.attachments:
                    ctype = (a.content_type or "").lower()
                    if ctype.startswith("image/") or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                        has_image = True
                        break
                if has_image:
                    try:
                        await m.delete(reason=f"Delete Images button by {interaction.user}")
                        deleted += 1
                    except Exception:
                        skipped += 1
            await interaction.response.send_message(f"🗑️ Deleted {deleted} image messages. Skipped {skipped}.", ephemeral=True)
            try:
                await self.channel.send(
                    f"🗑️ **Image purge** requested by {interaction.user.mention}. Deleted {deleted}, skipped {skipped}."
                )
            except Exception:
                pass
        except Exception:
            await interaction.response.send_message("Failed to scan/delete messages (permissions or rate limit).", ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒")
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        cfg = tickets_cfg(self.cog.bot)
        staff_ids = _staff_role_ids_for_option(cfg, self.value)
        allowed = False
        if isinstance(interaction.user, discord.Member):
            if _member_is_staff(interaction.user, staff_ids):
                allowed = True
            elif cfg.get("allow_user_close") and interaction.user.id == self.opener_id:
                allowed = True
        if not allowed:
            await interaction.response.send_message("You are not allowed to close this ticket.", ephemeral=True)
            return
        await interaction.response.send_modal(CloseModal(self.cog, self.channel, self.opener_id, staff_ids))

# ---------- Cog ----------
class TicketChannelsCog(commands.Cog):
    """Panel → per-ticket channel (YYYYMM-last4UID-####). Verifications get Delete Images; VC verify creates a voice channel.
       Also supports $CONFIGTICKET$ (autorun from config) and $CONFIGTICKETCUSTOM$ (full bootstrap)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ===== Auto-config triggers via chat =====
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        content = (message.content or "").strip()
        # admin guard
        is_admin = isinstance(message.author, discord.Member) and message.author.guild_permissions.administrator

        if content == "$CONFIGTICKET$":
            if not is_admin:
                await message.channel.send("❌ Admins only.")
                return
            await self._autorun_from_config(message.channel)

        if content == "$CONFIGTICKETCUSTOM$":
            if not is_admin:
                await message.channel.send("❌ Admins only.")
                return
            await self._bootstrap_custom(message.channel)

    async def _autorun_from_config(self, reply_ch: discord.TextChannel):
        cfg = tickets_cfg(self.bot)
        panel = cfg.get("panel") or {}
        hub_id = panel.get("hub_channel_id")
        if not hub_id:
            await reply_ch.send("⚠️ `tickets.panel.hub_channel_id` is not set in config.")
            return
        guild = reply_ch.guild
        hub = resolve_channel_any(guild, hub_id)
        if not isinstance(hub, discord.TextChannel):
            await reply_ch.send("⚠️ Configured hub channel is invalid or missing.")
            return
        await self._post_panel(hub, panel.get("image_url") or "")
        await save_config(self.bot.config)
        await reply_ch.send(f"✅ Ticket panel posted in {hub.mention} from config.")

    async def _bootstrap_custom(self, reply_ch: discord.TextChannel):
        guild = reply_ch.guild
        cfg = tickets_cfg(self.bot)

        # 1) Create categories
        archive_cat = await _find_or_create_category(guild, "Archive")
        tickets_cat = await _find_or_create_category(guild, "Tickets")
        logs_cat = await _find_or_create_category(guild, "Muffins Logs")
        control_cat = await _find_or_create_category(guild, "Muffin Control")

        # 2) Create channels
        tickets_log = await _find_or_create_text(guild, logs_cat, "tickets-log")
        ticket_setup = await _find_or_create_text(guild, control_cat, "ticket-setup")

        # 3) Wire config
        cfg["log_channel_id"] = tickets_log.id
        cfg["archive"]["enabled"] = True
        cfg["archive"]["category_id"] = archive_cat.id
        cfg["panel"]["hub_channel_id"] = ticket_setup.id
        # Leave panel image/title/description/colors as-is; user can set later.
        # Set all options parent_category_id → Tickets
        for o in cfg.get("panel_options") or []:
            o["parent_category_id"] = tickets_cat.id

        await save_config(self.bot.config)

        # 4) Post setup console instructions
        panel = cfg.get("panel", {})
        colors = panel.get("colors") or []
        color = _hex_to_color(colors[0]) if colors else discord.Color.blurple()
        embed = discord.Embed(
            title="Ticket System Setup Console",
            description=(
                "Use the commands below to finish setup.\n\n"
                "**Step 1 – Staff roles (IDs will be captured):**\n"
                "`!ticketspanel_chan staff @Staff @SECURITY`\n\n"
                "**Step 2 – (Optional) Per-option staff roles:**\n"
                "`!ticketspanel_chan setoptroles id_verification @SECURITY`\n"
                "`!ticketspanel_chan setoptroles cross_verification @SECURITY`\n"
                "`!ticketspanel_chan setoptroles video_verification @SECURITY`\n\n"
                "**Step 3 – (Optional) Transcript format:**\n"
                "`!ticketspanel_chan setformat html`\n\n"
                "**Step 4 – Post panel here:**\n"
                "`!ticketspanel_chan`  (or)  `!ticketspanel_chan create #ticket-setup <image_url>`\n\n"
                "**Step 5 – Review options:**\n"
                "`!ticketspanel_chan listopts`"
            ),
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Wired Destinations",
            value=(
                f"- **Tickets Category:** {tickets_cat.mention}\n"
                f"- **Archive Category:** {archive_cat.mention}\n"
                f"- **Logs Channel:** {tickets_log.mention}\n"
                f"- **Setup Hub:** {ticket_setup.mention}\n"
            ),
            inline=False,
        )
        await ticket_setup.send(embed=embed)
        await reply_ch.send("✅ Custom bootstrap complete. Review instructions in **#ticket-setup**.", delete_after=10)

    # ===== Admin command(s) =====
    @commands.has_permissions(administrator=True)
    @commands.command(name="ticketspanel_chan")
    async def ticketspanel_chan(self, ctx: commands.Context, sub: str = None, arg1: str = None, arg2: str = None):
        """
        Usage:
          !ticketspanel_chan                # post panel from config.hub
          !ticketspanel_chan create <#hub> <image_url>
          !ticketspanel_chan staff <@Role …|id …|[Name] …>
          !ticketspanel_chan addopt Label|value|CODE|<cat_id>|🎫|Description
          !ticketspanel_chan setoptroles <value> <@Role …|id …|[Name] …>
          !ticketspanel_chan listopts
          !ticketspanel_chan setarchive <#category|id|none>
          !ticketspanel_chan setformat <html|json>
          !tickets report
        """
        cfg = tickets_cfg(self.bot)
        if not sub:
            panel = cfg.get("panel") or {}
            hub_id = panel.get("hub_channel_id")
            if hub_id:
                hub = resolve_channel_any(ctx.guild, hub_id)
                if isinstance(hub, discord.TextChannel):
                    await self._post_panel(hub, panel.get("image_url") or "")
                    await ctx.send(f"✅ Panel posted (from config) in {hub.mention}")
                    return
            await ctx.send(self.ticketspanel_chan.__doc__.strip())
            return

        if sub.lower() == "create":
            hub = resolve_channel_any(ctx.guild, arg1)
            if not isinstance(hub, discord.TextChannel):
                await ctx.send("Choose a hub **text** channel.")
                return
            await self._post_panel(hub, arg2 or "")
            await save_config(self.bot.config)
            await ctx.send(f"✅ Panel posted in {hub.mention}")
            return

        if sub.lower() == "staff":
            ids: List[int] = []
            for tok in (arg1 or "").replace(",", " ").split():
                r = resolve_role_any(ctx.guild, tok)
                if r:
                    ids.append(r.id)
            cfg["staff_role_ids"] = sorted(set(ids))
            await save_config(self.bot.config)
            await ctx.send(f"✅ Global staff roles set: {cfg['staff_role_ids']}")
            return

        if sub.lower() == "setoptroles":
            if not arg1:
                await ctx.send("Usage: `!ticketspanel_chan setoptroles <value> <roles...>`")
                return
            value = arg1
            opt = _option_for_value(cfg, value)
            if not opt:
                await ctx.send("Option not found.")
                return
            ids: List[int] = []
            for tok in (arg2 or "").replace(",", " ").split():
                r = resolve_role_any(ctx.guild, tok)
                if r:
                    ids.append(r.id)
            opt["staff_role_ids"] = sorted(set(ids))
            await save_config(self.bot.config)
            await ctx.send(f"✅ Staff roles for `{value}` set: {opt['staff_role_ids']}")
            return

        if sub.lower() == "addopt":
            if not arg1:
                await ctx.send("Format: `Label|value|CODE|123456789012345678|🎫|Short description`")
                return
            parts = [p.strip() for p in arg1.split("|")]
            if len(parts) < 6:
                await ctx.send("Need 6 fields separated by `|`.")
                return
            label, value, code, parent, emoji, desc = parts[:6]
            try:
                parent_id = int(parent)
            except ValueError:
                await ctx.send("Parent category id must be a number.")
                return
            cfg["panel_options"].append(
                {
                    "label": label,
                    "value": value,
                    "code": code,
                    "parent_category_id": parent_id,
                    "emoji": emoji,
                    "description": desc,
                    "staff_role_ids": [],
                    "roles_to_ping_ids": [],
                    "verification": False,
                    "open_voice": False,
                }
            )
            await save_config(self.bot.config)
            await ctx.send(f"✅ Added option `{label}` → `{value}`")
            return

        if sub.lower() == "listopts":
            opts = (cfg.get("panel_options") or [])
            if not opts:
                await ctx.send("No options configured.")
                return
            lines = [
                f"- **{o['label']}** ({o['value']}) code=`{o['code']}` parent=`{o['parent_category_id']}` "
                f"verification={o.get('verification', False)} voice={o.get('open_voice', False)} "
                f"staff_roles={o.get('staff_role_ids', [])} ping_roles={o.get('roles_to_ping_ids', [])}"
                for o in opts
            ]
            await ctx.send("\n".join(lines)[:1900])
            return

        if sub.lower() == "setarchive":
            if not arg1:
                await ctx.send("Usage: `!ticketspanel_chan setarchive <#category|id|none>`")
                return
            if arg1.lower() == "none":
                cfg["archive"]["enabled"] = False
                cfg["archive"]["category_id"] = None
            else:
                cat = resolve_channel_any(ctx.guild, arg1)
                if not isinstance(cat, discord.CategoryChannel):
                    await ctx.send("Provide a **category**.")
                    return
                cfg["archive"]["enabled"] = True
                cfg["archive"]["category_id"] = cat.id
            await save_config(self.bot.config)
            await ctx.send(f"✅ Archive: {cfg['archive']}")
            return

        if sub.lower() == "setformat":
            fmt = (arg1 or "").lower()
            if fmt not in {"html", "json"}:
                await ctx.send("Use `html` or `json`.")
                return
            cfg["transcripts"]["format"] = fmt
            await save_config(self.bot.config)
            await ctx.send(f"✅ Transcript format set to `{fmt}`")
            return

        await ctx.send("Unknown subcommand.")

    async def _post_panel(self, hub: discord.TextChannel, image_url: str):
        cfg = tickets_cfg(self.bot)
        panel = cfg.get("panel", {})
        title = panel.get("title") or "How can we help?"
        desc = panel.get("description") or "Pick a category below to open a ticket channel."
        colors = panel.get("colors") or []
        try:
            color = discord.Color(int(colors[0].lstrip("#"), 16)) if colors else discord.Color.blurple()
        except Exception:
            color = discord.Color.blurple()
        emb = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.now(timezone.utc))
        if image_url:
            emb.set_image(url=image_url)
        await hub.send(embed=emb, view=TicketPanelView(self, hub))

    async def _post_controls(self, ch: discord.abc.GuildChannel, opener: discord.Member, opt_value: str, verification: bool):
        await self._refresh_controls_embed(ch, opener_id=opener.id, value=opt_value, verification=verification)

    async def _refresh_controls_embed(self, ch: discord.abc.GuildChannel, *, opener_id: int, value: str, verification: bool):
        cfg = tickets_cfg(self.bot)
        meta = cfg.get("active", {}).get(str(ch.id), {})
        claimed_by = meta.get("claimed_by")
        claimed_tag = f"<@{claimed_by}>" if claimed_by else "_unclaimed_"
        info = discord.Embed(
            title="Ticket Controls",
            description=(f"**Opener:** <@{opener_id}>\n" f"**Status:** {'Claimed by ' + claimed_tag if claimed_by else 'Unclaimed'}\n" f"**Opened:** {meta.get('opened_at', '—')}"),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        view = TicketControlsView(self, ch, opener_id, value, verification)
        found = None
        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel)):
            async for m in ch.history(limit=15, oldest_first=False):
                if m.author.id == (self.bot.user.id if self.bot.user else 0):
                    for e in m.embeds:
                        if (e.title or "").lower() == "ticket controls":
                            found = m
                            break
                if found:
                    break
            if found:
                try:
                    await found.edit(embed=info, view=view)
                except Exception:
                    pass
            else:
                try:
                    await ch.send(embed=info, view=view)
                except Exception:
                    pass

    async def _handle_close(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel, opener_id: int, staff_ids: List[int], resolution: str):
        cfg = tickets_cfg(self.bot)
        fmt = (cfg.get("transcripts", {}) or {}).get("format", "html")
        if fmt == "html":
            fname, blob = await export_channel_html(channel)
        else:
            fname, blob = await export_channel_json(channel)

        transcript_msg_url = ""
        transcript_cdn_url = ""
        log_ch = resolve_channel_any(channel.guild, cfg.get("log_channel_id"))
        if isinstance(log_ch, discord.TextChannel):
            c_by = (cfg.get("active", {}).get(str(channel.id)) or {}).get("claimed_by")
            em = discord.Embed(
                title="Ticket Closed",
                description=(
                    f"Channel: {channel.mention}\n"
                    f"Opener: <@{opener_id}>\n"
                    f"Claimed by: {('<@'+str(c_by)+'>') if c_by else '—'}\n"
                    f"Closed by: {interaction.user.mention}\n"
                    f"When: {ts_fmt_iso(datetime.now(timezone.utc))}\n"
                    f"Resolution: {resolution or '—'}"
                ),
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            try:
                msg = await log_ch.send(content="📦 Transcript attached.", embed=em, file=discord.File(blob, filename=fname))
                transcript_msg_url = msg.jump_url
                if msg.attachments:
                    transcript_cdn_url = msg.attachments[0].url
            except Exception:
                pass

        opener = channel.guild.get_member(opener_id)
        if opener:
            try:
                if transcript_cdn_url:
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

        try:
            if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
                async for m in channel.history(limit=15, oldest_first=False):
                    if m.author.id == (self.bot.user.id if self.bot.user else 0):
                        for e in m.embeds:
                            if (e.title or "").lower() == "ticket controls":
                                try:
                                    await m.edit(view=None)
                                except Exception:
                                    pass
                                break
        except Exception:
            pass

        archive = cfg.get("archive", {}) or {}
        archived = False
        if archive.get("enabled") and isinstance(resolve_channel_any(channel.guild, archive.get("category_id")), discord.CategoryChannel):
            try:
                cat = resolve_channel_any(channel.guild, archive.get("category_id"))
                new_name = f"{(archive.get('rename_prefix') or 'closed-')}{channel.name}"[:100]
                await channel.edit(category=cat, name=new_name, reason="Ticket archived")
                overwrites = channel.overwrites
                everyone = channel.guild.default_role
                po = overwrites.get(everyone, discord.PermissionOverwrite())
                po.send_messages = False
                overwrites[everyone] = po
                opener_m = channel.guild.get_member(opener_id)
                if opener_m:
                    upo = overwrites.get(opener_m, discord.PermissionOverwrite())
                    upo.send_messages = False
                    overwrites[opener_m] = upo
                await channel.edit(overwrites=overwrites, reason="Ticket closed — lock channel")
                archived = True
                try:
                    await interaction.response.send_message("📦 Archived this ticket.", ephemeral=True)
                except Exception:
                    pass
            except Exception:
                try:
                    await interaction.response.send_message("Archive failed; deleting instead.", ephemeral=True)
                except Exception:
                    pass
                try:
                    await channel.delete(reason="Ticket closed (archive failed)")
                except Exception:
                    pass
        else:
            try:
                await interaction.response.send_message("🔒 Closing…", ephemeral=True)
            except Exception:
                pass
            try:
                await channel.delete(reason=f"Ticket closed by {interaction.user} — {resolution or '—'}")
            except Exception:
                pass

        for rec in reversed(cfg.get("records", [])):
            if rec.get("channel_id") == channel.id:
                rec["closed_at"] = now_iso()
                rec["transcript_msg_url"] = transcript_msg_url
                rec["transcript_cdn_url"] = transcript_cdn_url
                rec["archived"] = archived
                break
        cfg.get("active", {}).pop(str(channel.id), None)
        await save_config(self.bot.config)

    # ===== Admin: report (xlsx) =====
    @commands.has_permissions(administrator=True)
    @commands.command(name="tickets")
    async def tickets_cmd(self, ctx: commands.Context, sub: str = None):
        if sub != "report":
            await ctx.send("`!tickets report` — export XLSX.")
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
            "transcript_msg_url",
            "transcript_cdn_url",
            "archived",
            "claimed_by_name",
            "claimed_by_id",
            "closed_at (UTC yyyy-mm-dd hh:mm)",
        ]
        ws.append(hdr)
        for r in rows:
            def fmt(s: str) -> str:
                try:
                    return ts_fmt_iso(datetime.fromisoformat(s.replace("Z", "+00:00")))
                except Exception:
                    return s or ""

            ws.append(
                [
                    fmt(r.get("opened_at", "")),
                    r.get("option", ""),
                    r.get("opener_name", ""),
                    r.get("opener_id", ""),
                    r.get("channel_id", ""),
                    r.get("transcript_msg_url", ""),
                    r.get("transcript_cdn_url", ""),
                    "TRUE" if r.get("archived") else "FALSE",
                    r.get("claimed_by_name", ""),
                    r.get("claimed_by_id", ""),
                    fmt(r.get("closed_at", "")),
                ]
            )
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = 24
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:K{ws.max_row}"
        ws["A1"].font = ws["B1"].font = Font(bold=True)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        await ctx.send(file=discord.File(buf, filename=f"tickets_report_{ctx.guild.id}_{yyyymm()}.xlsx"))

async def setup(bot: commands.Bot):
    await bot.add_cog(TicketChannelsCog(bot))
