# Feral_Kitty_FiFi/features/tickets_channels.py
from __future__ import annotations

import io
import csv
import html
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any
from ..config import save_config

try:
    import openpyxl  # optional; used by !tickets_report_xlsx
    from openpyxl.styles import Font
except Exception:
    openpyxl = None  # optional


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
        s = (s or '').strip()
        if not s:
            return default
        s = s.lower().replace('#', '').replace('0x', '')
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
    return ' '.join(out)

def _as_text_or_file(text: str) -> Tuple[Optional[str], Optional[discord.File]]:
    if len(text) <= 1900:
        return text, None
    buf = io.BytesIO(text.encode('utf-8'))
    buf.seek(0)
    return '📄 Output was long; attached as file.', discord.File(buf, filename='output.txt')


# ----------------------------
# Config helpers
# ----------------------------
def tickets_cfg(bot: commands.Bot) -> Dict[str, Any]:
    cfg = bot.config.setdefault('tickets', {})
    cfg.setdefault('log_channel_id', None)
    cfg.setdefault('staff_role_ids', [])  # preferred: IDs
    cfg.setdefault('roles_to_ping_names', [])  # fallback by names
    cfg.setdefault('panel', {
        'hub_channel_id': None,
        'image_url': '',
        'title': 'How can we help?',
        'description': 'Pick a category below to open a ticket channel.',
        'colors': ['#5865F2'],
        # we'll also store 'message_id' once we post the panel
    })
    cfg.setdefault('panel_options', [])  # up to 6 shown
    cfg.setdefault('delete_images', {'scan_limit': 500})
    cfg.setdefault('counters', {})
    cfg.setdefault('active', {})
    cfg.setdefault('records', [])
    cfg.setdefault('allow_user_close', False)
    cfg.setdefault('archive', {'enabled': True, 'category_id': None, 'rename_prefix': 'closed-'})
    cfg.setdefault('transcripts', {'format': 'html'})
    _ensure_ticket_defaults(cfg)
    return cfg

def _ensure_ticket_defaults(cfg: Dict[str, Any]) -> None:
    if 'panel_options' not in cfg or not isinstance(cfg['panel_options'], list):
        cfg['panel_options'] = []

    existing = {str(o.get('value', '')).lower() for o in cfg['panel_options']}
    defaults = [
        {'label': 'ID VERIFY', 'value': 'id_verification', 'code': 'IDV', 'parent_category_id': None, 'emoji': '🪪',
         'description': 'Upload a valid ID for age verification.', 'verification': True, 'open_voice': False, 'staff_role_ids': []},
        {'label': 'CROSS VERIFY', 'value': 'cross_verification', 'code': 'XVER', 'parent_category_id': None, 'emoji': '🧩',
         'description': 'Request cross verification from another server.', 'verification': True, 'open_voice': False, 'staff_role_ids': []},
        {'label': 'VC VERIFY', 'value': 'video_verification', 'code': 'VVER', 'parent_category_id': None, 'emoji': '🎥',
         'description': 'Verify your age over a quick video call.', 'verification': True, 'open_voice': True, 'staff_role_ids': []},
        {'label': 'REPORT', 'value': 'report', 'code': 'RPT', 'parent_category_id': None, 'emoji': '🚨',
         'description': 'Report an issue, request a DNI, or flag safety/security concerns.', 'verification': False, 'open_voice': False, 'staff_role_ids': []},
        {'label': 'PARTNERSHIP', 'value': 'partnership', 'code': 'PART', 'parent_category_id': None, 'emoji': '🤝',
         'description': 'Request a partnership review with our team.', 'verification': False, 'open_voice': False, 'staff_role_ids': []},
        {'label': 'PROMOTION', 'value': 'promotion', 'code': 'PRM', 'parent_category_id': None, 'emoji': '📣',
         'description': 'Request promo for events, socials, streaming, artwork, or adult links.', 'verification': False, 'open_voice': False, 'staff_role_ids': []},
    ]
    for d in defaults:
        if d['value'].lower() not in existing:
            cfg['panel_options'].append(d)

    seen = set()
    unique = []
    for o in cfg['panel_options']:
        v = str(o.get('value', '')).lower()
        if v not in seen:
            seen.add(v)
            unique.append(o)
    cfg['panel_options'] = unique

def _option_for_value(cfg: Dict[str, Any], value: str) -> Optional[Dict[str, Any]]:
    value = str(value).lower()
    for o in (cfg.get('panel_options') or []):
        if str(o.get('value', '')).lower() == value:
            return o
    return None

def _resolve_staff_role_ids(guild: discord.Guild, cfg: Dict[str, Any]) -> List[int]:
    # Prefer explicit IDs; fallback to names
    ids = [int(x) for x in (cfg.get('staff_role_ids') or []) if isinstance(x, int) or str(x).isdigit()]
    ids = [i for i in ids if guild.get_role(i)]
    if ids:
        return sorted(set(ids))
    names = cfg.get('roles_to_ping_names') or []
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
    def __init__(self, cog: 'TicketChannelsCog', hub: discord.TextChannel):
        self.cog = cog
        self.hub = hub
        cfg = tickets_cfg(cog.bot)

        options: List[discord.SelectOption] = []
        for o in (cfg.get('panel_options') or [])[:6]:
            options.append(
                discord.SelectOption(
                    label=str(o.get('label') or o.get('value') or 'Option')[:100],
                    value=str(o.get('value') or '')[:100],
                    description=(str(o.get('description') or '')[:95] or None),
                    emoji=o.get('emoji') or None,
                )
            )

        super().__init__(
            placeholder='Select a ticket type…',
            min_values=1,
            max_values=1,
            options=options,
            disabled=not bool(options),
            custom_id='tickets:panel_select',
        )

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        if not interaction.guild or not isinstance(user, discord.Member):
            return await interaction.response.send_message('Only server members can open tickets.', ephemeral=True)

        cfg = tickets_cfg(self.cog.bot)
        value = self.values[0]
        opt = _option_for_value(cfg, value)
        if not opt:
            return await interaction.response.send_message('That ticket option is unavailable.', ephemeral=True)

        parent_id = opt.get('parent_category_id')
        parent = interaction.guild.get_channel(int(parent_id)) if parent_id else None
        if parent_id and not isinstance(parent, discord.CategoryChannel):
            return await interaction.response.send_message('Configured ticket category is invalid.', ephemeral=True)

        # resolve staff ids
        opt_ids = [int(x) for x in (opt.get('staff_role_ids') or []) if isinstance(x, int) or str(x).isdigit()]
        opt_ids = [i for i in opt_ids if interaction.guild.get_role(i)]
        staff_ids = sorted(set(opt_ids)) if opt_ids else _resolve_staff_role_ids(interaction.guild, cfg)

        verification = bool(opt.get('verification', False))
        open_voice = bool(opt.get('open_voice', False))

        # Name: YYYYMM-last4UID-#### (no username)
        last4 = f"{user.id % 10000:04d}"
        seq_bucket = cfg.setdefault('counters', {}).setdefault(yyyymm(), {})
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
                name=base_name, category=parent, overwrites=overwrites or None, reason=f'Ticket opened by {user} ({value})'
            )
        except discord.Forbidden:
            return await interaction.response.send_message('I lack permission to create ticket channels.', ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f'Failed to create ticket: {e}', ephemeral=True)

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
                    name=f"{base_name}-vc"[:100], category=parent, overwrites=v_overwrites or None, reason=f'Ticket voice opened by {user} ({value})'
                )
            except Exception:
                voice_ch = None  # non-fatal

        # record
        rec = {
            'opened_at': now_iso(),
            'category': value,
            'opener_name': str(user),
            'opener_id': user.id,
            'channel_id': text_ch.id,
            'voice_channel_id': voice_ch.id if voice_ch else None,
            'claimed_by_name': '',
            'claimed_by_id': None,
            'claimed_at': '',
            'closed_at': '',
            'transcript_msg_url': '',
            'transcript_cdn_url': '',
            'archived': False,
        }
        cfg.setdefault('records', []).append(rec)
        cfg.setdefault('active', {})[str(text_ch.id)] = {
            'opener_id': user.id,
            'value': value,
            'opened_at': rec['opened_at'],
            'claimed_by': None,
            'claimed_at': None,
            'verification': verification,
            'voice_channel_id': voice_ch.id if voice_ch else None,
        }
        await save_config(self.cog.bot.config)

        # intro + ping
        mentions = []
        for rid in staff_ids:
            r = interaction.guild.get_role(rid)
            if r:
                mentions.append(r.mention)

        label = opt.get('label') or value
        intro = discord.Embed(
            title=f'Ticket: {label}',
            description=(
                f'Opened by {user.mention} • {ts_fmt(datetime.now(timezone.utc))}\n\n'
                + ("🎥 **Voice channel created:** " + (voice_ch.mention if voice_ch else '_failed to create_') + "\n\n" if open_voice else '')
                + 'Provide details below.'
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        try:
            if mentions:
                await text_ch.send(content=safe_join_mentions(mentions), embed=intro,
                                   allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))
            else:
                await text_ch.send(embed=intro)
        except Exception:
            pass

        await interaction.response.send_message(f'✅ Ticket created: {text_ch.mention}', ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: 'TicketChannelsCog', hub: discord.TextChannel):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(cog, hub))


# ----------------------------
# Logging & transcripts helpers
# ----------------------------
def _get_log_channel(guild: discord.Guild, cfg: Dict[str, Any]) -> Optional[discord.TextChannel]:
    ch_id = (cfg or {}).get('log_channel_id')
    if not ch_id:
        return None
    ch = guild.get_channel(int(ch_id)) if str(ch_id).isdigit() else None
    return ch if isinstance(ch, discord.TextChannel) else None

async def _log(guild: discord.Guild, cfg: Dict[str, Any], content: str, embed: Optional[discord.Embed] = None):
    ch = _get_log_channel(guild, cfg)
    if not ch:
        return
    try:
        await ch.send(content=content, embed=embed)
    except Exception:
        pass

async def _generate_transcript_html(channel: discord.TextChannel, limit: int = 2000) -> bytes:
    msgs = []
    async for m in channel.history(limit=limit, oldest_first=True):
        ts = ts_fmt(m.created_at)
        author = html.escape(f"{m.author} ({m.author.id})")
        content = html.escape(m.content or '')
        attachments = ''
        if m.attachments:
            items = [f'<li><a href="{html.escape(a.url)}" target="_blank">{html.escape(a.filename)}</a></li>' for a in m.attachments]
            attachments = f"<ul>{''.join(items)}</ul>"
        msgs.append(f"""
        <div class="msg">
          <div class="meta"><b>{author}</b> <span>{ts}</span></div>
          <div class="body">{content}</div>
          {attachments}
        </div>
        """)
    html_doc = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Transcript - {html.escape(channel.name)}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 16px; }}
.msg {{ border-bottom: 1px solid #eee; padding: 8px 0; }}
.meta {{ color: #666; font-size: 12px; }}
.body {{ white-space: pre-wrap; margin-top: 4px; }}
</style>
</head><body>
<h2>Transcript: #{html.escape(channel.name)}</h2>
<p>Guild: {html.escape(channel.guild.name)} • Channel ID: {channel.id}</p>
{"".join(msgs)}
</body></html>"""
    return html_doc.encode('utf-8')


# ----------------------------
# Cog (panel + full ops)
# ----------------------------
class TicketChannelsCog(commands.Cog):
    """Ticket panel + creation + full operations (claim/unclaim/close/reopen/transcript/report/purge)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---- post panel ----
    async def _post_panel(self, hub: discord.TextChannel):
        cfg = tickets_cfg(self.bot)
        panel = cfg.get('panel') or {}
        title = panel.get('title') or 'How can we help?'
        desc = panel.get('description') or 'Pick a category below to open a ticket channel.'
        image_url = panel.get('image_url') or ''
        colors = panel.get('colors') or ['#5865F2']
        color_val = parse_hex_color(colors[0] if colors else '#5865F2')

        view = TicketPanelView(self, hub)
        emb = discord.Embed(
            title=str(title)[:256],
            description=str(desc)[:4096],
            color=color_val,
            timestamp=datetime.now(timezone.utc),
        )
        if image_url:
            emb.set_image(url=image_url)

        # Idempotent panel: update if message_id known; else send and save id
        panel_cfg = cfg.setdefault('panel', {})
        msg_id = panel_cfg.get('message_id')
        if msg_id:
            try:
                msg = await hub.fetch_message(int(msg_id))
                await msg.edit(embed=emb, view=view)
                return
            except Exception:
                pass
        sent = await hub.send(embed=emb, view=view)
        panel_cfg['message_id'] = sent.id
        await save_config(self.bot.config)

    # ---- admin command ----
    @commands.has_permissions(administrator=True)
    @commands.command(name='ticketspanel_chan')
    async def ticketspanel_chan(self, ctx: commands.Context, sub: str = None, *, rest: str = ''):
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
            panel = cfg.get('panel') or {}
            hub_id = panel.get('hub_channel_id')
            hub = resolve_channel_any(ctx.guild, hub_id)
            if not isinstance(hub, discord.TextChannel):
                await ctx.send('❌ tickets.panel.hub_channel_id is not set to a valid text channel in config.')
                return
            await self._post_panel(hub)
            await ctx.send(f'✅ Panel posted in {hub.mention}.')
            return

        sub_l = sub.lower().strip()

        if sub_l == 'create':
            parts = (rest or '').split()
            if not parts:
                await ctx.send('Usage: `!ticketspanel_chan create #channel [image_url]`')
                return
            hub = resolve_channel_any(ctx.guild, parts[0])
            if not isinstance(hub, discord.TextChannel):
                await ctx.send('❌ Provide a valid text channel for the hub.')
                return
            image_url = parts[1] if len(parts) > 1 else (cfg.get('panel', {}) or {}).get('image_url', '')

            cfg.setdefault('panel', {})['hub_channel_id'] = hub.id
            cfg.setdefault('panel', {})['image_url'] = image_url
            await save_config(self.bot.config)

            await self._post_panel(hub)
            await ctx.send(f'✅ Panel posted and saved to config in {hub.mention}.')
            return

        if sub_l == 'listopts':
            opts = cfg.get('panel_options') or []
            if not opts:
                await ctx.send('ℹ️ No options configured.')
                return
            lines = []
            for o in opts:
                lines.append(
                    f"- **{o.get('label')}** (`{o.get('value')}`) "
                    f"code=`{o.get('code')}` parent=`{o.get('parent_category_id')}` "
                    f"verification={o.get('verification', False)} voice={o.get('open_voice', False)} "
                    f"roles={o.get('staff_role_ids', [])}"
                )
            text = '\n'.join(lines)
            msg, f = _as_text_or_file(text)
            await ctx.send(content=msg, file=f)
            return

        await ctx.send('Usage: `!ticketspanel_chan` | `!ticketspanel_chan create #hub [image_url]` | `!ticketspanel_chan listopts`')

    # =========================
    # Ticket operations
    # =========================
    async def _is_staff(self, member: discord.Member) -> bool:
        cfg = tickets_cfg(self.bot)
        ids = _resolve_staff_role_ids(member.guild, cfg)
        return any((member.get_role(rid) is not None) for rid in ids) or member.guild_permissions.administrator

    @commands.command(name='ticket_claim')
    async def ticket_claim(self, ctx: commands.Context):
        """Claim the current ticket channel (staff only)."""
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.reply('Run this in a ticket text channel.')
        cfg = tickets_cfg(self.bot)
        act = cfg.get('active', {}).get(str(ctx.channel.id))
        if not act:
            return await ctx.reply('This channel is not tracked as a ticket.')
        if not await self._is_staff(ctx.author):
            return await ctx.reply('Only staff can claim tickets.')
        if act.get('claimed_by'):
            return await ctx.reply(f"Already claimed by <@{act['claimed_by']}>.")
        act['claimed_by'] = ctx.author.id
        act['claimed_at'] = now_iso()
        for r in cfg.get('records', []):
            if r.get('channel_id') == ctx.channel.id:
                r['claimed_by_name'] = str(ctx.author)
                r['claimed_by_id'] = ctx.author.id
                r['claimed_at'] = act['claimed_at']
                break
        await save_config(self.bot.config)
        await ctx.reply(f'✅ Claimed by {ctx.author.mention}.')
        await _log(ctx.guild, cfg, f'🧩 Ticket claimed: {ctx.channel.mention} by {ctx.author.mention}')

    @commands.command(name='ticket_unclaim')
    async def ticket_unclaim(self, ctx: commands.Context):
        """Unclaim the current ticket (staff only)."""
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.reply('Run this in a ticket text channel.')
        cfg = tickets_cfg(self.bot)
        act = cfg.get('active', {}).get(str(ctx.channel.id))
        if not act:
            return await ctx.reply('This channel is not tracked as a ticket.')
        if not await self._is_staff(ctx.author):
            return await ctx.reply('Only staff can unclaim tickets.')
        if not act.get('claimed_by'):
            return await ctx.reply('Ticket is not claimed.')
        act['claimed_by'] = None
        act['claimed_at'] = None
        for r in cfg.get('records', []):
            if r.get('channel_id') == ctx.channel.id:
                r['claimed_by_name'] = ''
                r['claimed_by_id'] = None
                r['claimed_at'] = ''
                break
        await save_config(self.bot.config)
        await ctx.reply('✅ Unclaimed.')
        await _log(ctx.guild, cfg, f'♻️ Ticket unclaimed: {ctx.channel.mention} by {ctx.author.mention}')

    @commands.command(name='ticket_transcript')
    async def ticket_transcript(self, ctx: commands.Context, limit: int = 2000):
        """Generate and upload an HTML transcript of this ticket."""
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.reply('Run this in a ticket text channel.')
        cfg = tickets_cfg(self.bot)
        act = cfg.get('active', {}).get(str(ctx.channel.id))
        if not act:
            return await ctx.reply('This channel is not tracked as a ticket.')
        if not (await self._is_staff(ctx.author) or act.get('opener_id') == ctx.author.id):
            return await ctx.reply('You must be staff or the ticket opener.')
        data = await _generate_transcript_html(ctx.channel, limit=limit)
        buf = io.BytesIO(data); buf.seek(0)
        file = discord.File(buf, filename=f'transcript_{ctx.channel.id}.html')
        msg = await ctx.channel.send(content='📄 Transcript generated.', file=file)
        for r in cfg.get('records', []):
            if r.get('channel_id') == ctx.channel.id:
                r['transcript_msg_url'] = msg.jump_url
                break
        await save_config(self.bot.config)
        await _log(ctx.guild, cfg, f'🧾 Transcript generated for {ctx.channel.mention}: {msg.jump_url}')

    @commands.command(name='ticket_close')
    async def ticket_close(self, ctx: commands.Context, *, reason: str = ''):
        """Close the ticket: transcript, rename, move to archive (if configured), cleanup voice channel."""
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.reply('Run this in a ticket text channel.')
        cfg = tickets_cfg(self.bot)
        act = cfg.get('active', {}).get(str(ctx.channel.id))
        if not act:
            return await ctx.reply('This channel is not tracked as a ticket.')
        allow_user_close = bool(cfg.get('allow_user_close', False))
        is_staff = await self._is_staff(ctx.author)
        if not (is_staff or (allow_user_close and act.get('opener_id') == ctx.author.id)):
            return await ctx.reply('You do not have permission to close this ticket.')

        # transcript best-effort
        try:
            data = await _generate_transcript_html(ctx.channel, limit=2000)
            buf = io.BytesIO(data); buf.seek(0)
            f = discord.File(buf, filename=f'transcript_{ctx.channel.id}.html')
            msg = await ctx.channel.send(content='📄 Transcript (auto on close).', file=f)
            for r in cfg.get('records', []):
                if r.get('channel_id') == ctx.channel.id:
                    r['transcript_msg_url'] = msg.jump_url
                    r['closed_at'] = now_iso()
                    break
        except Exception:
            for r in cfg.get('records', []):
                if r.get('channel_id') == ctx.channel.id:
                    r['closed_at'] = now_iso()
                    break

        # archive handling
        arch_cfg = cfg.get('archive') or {}
        do_archive = bool(arch_cfg.get('enabled', True))
        rename_prefix = str(arch_cfg.get('rename_prefix') or 'closed-')
        target_cat_id = arch_cfg.get('category_id')
        target_cat = ctx.guild.get_channel(int(target_cat_id)) if target_cat_id else None
        try:
            if do_archive:
                new_name = (rename_prefix + ctx.channel.name)[:100]
                if isinstance(target_cat, discord.CategoryChannel) and ctx.channel.category_id != target_cat.id:
                    await ctx.channel.edit(name=new_name, category=target_cat, reason=f'Ticket closed by {ctx.author} {reason}')
                else:
                    await ctx.channel.edit(name=new_name, reason=f'Ticket closed by {ctx.author} {reason}')
        except discord.Forbidden:
            await ctx.reply('⚠️ I could not move/rename the channel due to permissions.')
        except Exception:
            pass

        # delete orphaned voice channel if any
        vch_id = act.get('voice_channel_id')
        if vch_id:
            vch = ctx.guild.get_channel(int(vch_id))
            if isinstance(vch, discord.VoiceChannel):
                try:
                    await vch.delete(reason='Ticket closed; cleaning up voice channel')
                except Exception:
                    pass

        # mark archived
        for r in cfg.get('records', []):
            if r.get('channel_id') == ctx.channel.id:
                r['archived'] = True
                break
        cfg.get('active', {}).pop(str(ctx.channel.id), None)
        await save_config(self.bot.config)
        await ctx.reply('✅ Ticket closed.' + (f' Reason: {reason}' if reason else ''))
        await _log(ctx.guild, cfg, f'🗄️ Ticket closed: {ctx.channel.mention} by {ctx.author.mention} ' + (f'– {reason}' if reason else ''))

    @commands.command(name='ticket_reopen')
    async def ticket_reopen(self, ctx: commands.Context):
        """Reopen a closed ticket: remove archive prefix and move back to parent category if defined."""
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.reply('Run this in a ticket text channel.')
        cfg = tickets_cfg(self.bot)
        records = cfg.get('records', [])
        rec = next((r for r in records if r.get('channel_id') == ctx.channel.id), None)
        if not rec:
            return await ctx.reply('No ticket record found for this channel.')
        if not await self._is_staff(ctx.author):
            return await ctx.reply('Only staff can reopen tickets.')

        # restore name
        arch_cfg = cfg.get('archive') or {}
        prefix = str(arch_cfg.get('rename_prefix') or 'closed-')
        new_name = ctx.channel.name
        if new_name.startswith(prefix):
            new_name = new_name[len(prefix):]

        # move back to option parent category if exists
        opt = _option_for_value(cfg, rec.get('category', ''))
        dest_cat = None
        if opt and opt.get('parent_category_id'):
            ch = ctx.guild.get_channel(int(opt.get('parent_category_id')))
            if isinstance(ch, discord.CategoryChannel):
                dest_cat = ch
        try:
            await ctx.channel.edit(name=new_name[:100], category=dest_cat, reason=f'Reopened by {ctx.author}')
        except Exception:
            pass

        # mark active (unclaimed by default)
        cfg.setdefault('active', {})[str(ctx.channel.id)] = {
            'opener_id': rec.get('opener_id'),
            'value': rec.get('category'),
            'opened_at': rec.get('opened_at'),
            'claimed_by': None,
            'claimed_at': None,
            'verification': bool((opt or {}).get('verification', False)),
            'voice_channel_id': None,
        }
        rec['archived'] = False
        rec['closed_at'] = ''
        await save_config(self.bot.config)
        await ctx.reply('✅ Ticket reopened.')
        await _log(ctx.guild, cfg, f'🔁 Ticket reopened: {ctx.channel.mention} by {ctx.author.mention}')

    @commands.has_permissions(manage_messages=True)
    @commands.command(name='tickets_purge_images')
    async def tickets_purge_images(self, ctx: commands.Context, scan_limit: Optional[int] = None):
        """Delete recent non-bot messages with attachments in this ticket (preserves transcript files)."""
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.reply('Run this in a ticket text channel.')
        cfg = tickets_cfg(self.bot)
        lim = int(scan_limit or cfg.get('delete_images', {}).get('scan_limit', 500))
        deleted = 0
        async for msg in ctx.channel.history(limit=lim, oldest_first=False):
            if msg.attachments and not msg.author.bot:
                keep = any((a.filename or '').lower().startswith('transcript_') and (a.filename or '').lower().endswith('.html')
                           for a in msg.attachments)
                if keep:
                    continue
                try:
                    await msg.delete()
                    deleted += 1
                    await asyncio.sleep(0.6)  # be gentle
                except Exception:
                    pass
        await ctx.reply(f'🧹 Deleted {deleted} message(s) with attachments in the last {lim} messages.')

    # =========================
    # Admin helpers & reports
    # =========================
    @commands.has_permissions(administrator=True)
    @commands.command(name='tickets_set_log')
    async def tickets_set_log(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Set the tickets log channel. Usage: !tickets_set_log #channel or run in target channel."""
        cfg = tickets_cfg(self.bot)
        target = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        if not isinstance(target, discord.TextChannel):
            return await ctx.reply('Provide a log text channel or run this in the desired channel.')
        cfg['log_channel_id'] = target.id
        await save_config(self.bot.config)
        await ctx.reply(f'✅ Log channel set to {target.mention}.')

    @commands.has_permissions(administrator=True)
    @commands.command(name='tickets_set_archive')
    async def tickets_set_archive(self, ctx: commands.Context, category: Optional[discord.CategoryChannel] = None, rename_prefix: Optional[str] = None):
        """Set archive category and optional rename prefix. Usage: !tickets_set_archive <#category> [prefix]"""
        cfg = tickets_cfg(self.bot)
        if not category:
            return await ctx.reply('Please mention a category channel.')
        cfg.setdefault('archive', {})['category_id'] = category.id
        if rename_prefix is not None:
            cfg.setdefault('archive', {})['rename_prefix'] = rename_prefix
        await save_config(self.bot.config)
        await ctx.reply(f'✅ Archive category set to **{category.name}** with prefix `{cfg["archive"]["rename_prefix"]}`.')

    @commands.has_permissions(administrator=True)
    @commands.command(name='tickets_allow_user_close')
    async def tickets_allow_user_close(self, ctx: commands.Context, value: Optional[bool] = None):
        """Toggle whether openers can close their own tickets. Usage: !tickets_allow_user_close true|false"""
        cfg = tickets_cfg(self.bot)
        if value is None:
            return await ctx.reply(f'Current: `{cfg.get("allow_user_close", False)}`. Pass true/false to change.')
        cfg['allow_user_close'] = bool(value)
        await save_config(self.bot.config)
        await ctx.reply(f'✅ allow_user_close set to `{cfg["allow_user_close"]}`.')

    @commands.has_permissions(administrator=True)
    @commands.command(name='tickets_report')
    async def tickets_report(self, ctx: commands.Context):
        """Generate a CSV report of all ticket records."""
        cfg = tickets_cfg(self.bot)
        records = cfg.get('records', [])
        if not records:
            return await ctx.reply('No ticket records found.')
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            'opened_at','category','opener_name','opener_id',
            'channel_id','voice_channel_id',
            'claimed_by_name','claimed_by_id','claimed_at',
            'closed_at','transcript_msg_url','archived'
        ])
        for r in records:
            writer.writerow([
                r.get('opened_at',''), r.get('category',''),
                r.get('opener_name',''), r.get('opener_id',''),
                r.get('channel_id',''), r.get('voice_channel_id',''),
                r.get('claimed_by_name',''), r.get('claimed_by_id',''), r.get('claimed_at',''),
                r.get('closed_at',''), r.get('transcript_msg_url',''), r.get('archived', False)
            ])
        data = buf.getvalue().encode('utf-8')
        f = discord.File(io.BytesIO(data), filename=f'tickets_report_{yyyymm()}.csv')
        await ctx.reply(file=f)

    @commands.has_permissions(administrator=True)
    @commands.command(name='tickets_report_xlsx')
    async def tickets_report_xlsx(self, ctx: commands.Context):
        """Generate an XLSX report (if openpyxl is available)."""
        if openpyxl is None:
            return await ctx.reply('openpyxl is not installed on this runtime; use !tickets_report for CSV.')
        cfg = tickets_cfg(self.bot)
        records = cfg.get('records', [])
        if not records:
            return await ctx.reply('No ticket records found.')
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Tickets'
        headers = [
            'opened_at','category','opener_name','opener_id',
            'channel_id','voice_channel_id',
            'claimed_by_name','claimed_by_id','claimed_at',
            'closed_at','transcript_msg_url','archived'
        ]
        ws.append(headers)
        for c in range(1, len(headers)+1):
            ws.cell(row=1, column=c).font = Font(bold=True)
        for r in records:
            ws.append([
                r.get('opened_at',''), r.get('category',''),
                r.get('opener_name',''), r.get('opener_id',''),
                r.get('channel_id',''), r.get('voice_channel_id',''),
                r.get('claimed_by_name',''), r.get('claimed_by_id',''), r.get('claimed_at',''),
                r.get('closed_at',''), r.get('transcript_msg_url',''), r.get('archived', False)
            ])
        out = io.BytesIO()
        wb.save(out)
        out.seek(0)
        await ctx.reply(file=discord.File(out, filename=f'tickets_report_{yyyymm()}.xlsx'))


# ----------------------------
# Setup entry point
# ----------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(TicketChannelsCog(bot))
