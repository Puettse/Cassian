# feral_kitty_fifi/features/safeword.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional, List, Tuple
import io, json, asyncio
from datetime import datetime, timezone
import discord
from discord.ext import commands
from ..utils.discord_resolvers import resolve_role_any, resolve_channel_any, normalize
from ..utils.io_helpers import now_iso, aio_retry
from ..utils.perms import can_manage_role, staff_check_factory

@dataclass
class LockSnapshot:
    prior_send_everyone: Optional[bool]
    prior_slowmode: Optional[int]

class Safeword(commands.Cog):
    """Safeword handling: !STOP! / !Release, locking, pings, export, thanos."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock_state: Dict[int, LockSnapshot] = {}
        self._last_trigger_at: Dict[int, float] = {}
        self._staff_check = staff_check_factory(lambda: self.bot.config)

    def _sw_cfg(self) -> Dict[str, Any]:
        return (self.bot.config or {}).get("safeword") or {}

    def _member_authorized(self, member: discord.Member, guild: discord.Guild, roles_whitelist: List[Any]) -> bool:
        if not roles_whitelist: roles_whitelist = ["Staff"]
        ids = {rid for rid in roles_whitelist if isinstance(rid, int)}
        names = {normalize(rn) for rn in roles_whitelist if isinstance(rn, str)}
        return any(r.id in ids or normalize(r.name) in names for r in member.roles)

    def _member_blocked(self, member: discord.Member, blocked_roles: List[Any]) -> bool:
        if not blocked_roles: return False
        ids = {rid for rid in blocked_roles if isinstance(rid, int)}
        names = {normalize(rn) for rn in blocked_roles if isinstance(rn, str)}
        return any((r.id in ids) or (normalize(r.name) in names) for r in member.roles)

    async def _export_last_messages_json(self, channel: discord.TextChannel, limit: int) -> Tuple[str, io.BytesIO]:
        msgs = []
        async for m in channel.history(limit=max(1, min(100, limit)), oldest_first=False):
            msgs.append({
                "id": m.id,
                "author": {"id": m.author.id, "name": f"{m.author}", "bot": bool(getattr(m.author, 'bot', False))},
                "created_at_iso": m.created_at.replace(tzinfo=timezone.utc).isoformat(),
                "content": m.content,
                "attachments": [{"id": a.id, "filename": a.filename, "url": a.url, "size": a.size} for a in m.attachments],
                "embeds": [{"type": e.type, "title": getattr(e, 'title', None), "description": getattr(e, 'description', None)} for e in m.embeds],
                "reference": {"message_id": getattr(m.reference, 'message_id', None)} if m.reference else None,
                "jump_url": m.jump_url,
            })
        payload = {"channel": {"id": channel.id, "name": channel.name}, "exported_at_iso": now_iso(), "count": len(msgs), "messages": msgs}
        buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        fname = f"safeword_{channel.id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        buf.seek(0)
        return fname, buf

    async def _lock_channel(self, channel: discord.TextChannel, roles_whitelist: List[Any]) -> Optional[str]:
        guild = channel.guild
        everyone = guild.default_role
        prior = channel.overwrites_for(everyone).send_messages
        prior_slow = channel.slowmode_delay
        self._lock_state[channel.id] = LockSnapshot(prior_send_everyone=prior, prior_slowmode=prior_slow)
        try:
            await aio_retry(lambda: channel.set_permissions(everyone, send_messages=False, reason="Safeword lock"), ctx="lock-deny")
            for token in roles_whitelist or []:
                role = resolve_role_any(guild, token)
                if role:
                    await aio_retry(lambda r=role: channel.set_permissions(r, send_messages=True, reason="Safeword whitelist"), ctx="lock-allow")
            await aio_retry(lambda: channel.edit(slowmode_delay=1800, reason="Safeword 30m slowmode"), ctx="lock-slowmode")
            return None
        except Exception:
            return "lock-error"

    async def _unlock_channel(self, channel: discord.TextChannel) -> Optional[str]:
        guild = channel.guild
        everyone = guild.default_role
        snap = self._lock_state.get(channel.id, LockSnapshot(prior_send_everyone=None, prior_slowmode=0))
        try:
            await aio_retry(lambda: channel.edit(slowmode_delay=snap.prior_slowmode or 0, reason="Safeword release"), ctx="unlock-slowmode")
            await aio_retry(lambda: channel.set_permissions(everyone, overwrite=None), ctx="unlock-clear")
            self._lock_state.pop(channel.id, None)
            return None
        except Exception:
            return "unlock-error"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            if message.author.bot or not message.guild:
                return
            sw = self._sw_cfg()
            if not sw.get("enabled", True):
                return
            content = message.content.strip()
            trig = (sw.get("trigger") or "!STOP!").strip()
            rtrig = (sw.get("release_trigger") or "!Release").strip()

            if content == trig:
                await self._handle_safeword(message)
                return
            if content == rtrig:
                await self._handle_release(message)
                return
        except Exception:
            pass
        
    async def _handle_safeword(self, message: discord.Message) -> None:
        cfg = self._sw_cfg()
        ch = message.channel
        if not isinstance(ch, discord.TextChannel):
            return
        if isinstance(message.author, discord.Member) and self._member_blocked(message.author, cfg.get("blocked_roles") or []):
            await ch.send("❌ You are not permitted to use this command.")
            return

        cd = int(cfg.get("cooldown_seconds") or 0)
        if cd > 0:
            last = self._last_trigger_at.get(ch.id, 0); now = asyncio.get_event_loop().time()
            if now - last < cd: return
            self._last_trigger_at[ch.id] = now

        mentions = []
        for token in cfg.get("roles_to_ping") or []:
            role = resolve_role_any(message.guild, token)
            if role: mentions.append(role.mention)
        if mentions:
            await aio_retry(lambda: ch.send(" ".join(mentions), allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False)), ctx="ping-roles")

        lock_msg = (cfg.get("lock_message") or {}).get("text") or ""
        lock_img = (cfg.get("lock_message") or {}).get("image_url") or ""
        embed = None
        if lock_img or lock_msg:
            embed = discord.Embed(description=lock_msg, color=discord.Color.red())
            if lock_img: embed.set_image(url=lock_img)
        await aio_retry(lambda: ch.send(lock_msg if not embed else None, embed=embed), ctx="lock-message")

        log_chan_id = cfg.get("log_channel_id"); history_limit = int(cfg.get("history_limit") or 25)
        if isinstance(log_chan_id, int) and log_chan_id > 0:
            log_ch = resolve_channel_any(message.guild, log_chan_id)
            if isinstance(log_ch, discord.TextChannel):
                fname, blob = await self._export_last_messages_json(ch, history_limit)
                em = discord.Embed(
                    title="Safeword Triggered",
                    description=f"Channel: {ch.mention}\nBy: {message.author.mention}\nWhen: {now_iso()}",
                    color=discord.Color.red(),
                )
                em.add_field(name="Jump", value=f"[link]({message.jump_url})", inline=False)
                await aio_retry(lambda: log_ch.send(content="📦 Transcript attached.", embed=em, file=discord.File(blob, filename=fname)), ctx="export-log")

        err = await self._lock_channel(ch, cfg.get("roles_whitelist") or ["Staff"])
        if err:
            await ch.send("❌ Failed to lock channel. Staff please review logs.")

    async def _handle_release(self, message: discord.Message) -> None:
        cfg = self._sw_cfg()
        ch = message.channel
        if not isinstance(ch, discord.TextChannel) or not isinstance(message.author, discord.Member):
            return
        if not self._member_authorized(message.author, message.guild, cfg.get("roles_whitelist") or ["Staff"]):
            await ch.send("❌ You do not have permission to release this channel.")
            return

        err = await self._unlock_channel(ch)
        rel_msg = (cfg.get("release_message") or {}).get("text") or ""
        rel_img = (cfg.get("release_message") or {}).get("image_url") or ""
        embed = discord.Embed(description=rel_msg, color=discord.Color.green())
        if rel_img: embed.set_image(url=rel_img)
        await ch.send(rel_msg if not embed else None, embed=embed)

        log_chan_id = cfg.get("log_channel_id")
        if isinstance(log_chan_id, int) and log_chan_id > 0:
            log_ch = resolve_channel_any(message.guild, log_chan_id)
            if isinstance(log_ch, discord.TextChannel):
                em = discord.Embed(
                    title="Safeword Release",
                    description=f"Channel: {ch.mention}\nBy: {message.author.mention}\nWhen: {now_iso()}",
                    color=discord.Color.green(),
                )
                await aio_retry(lambda: log_ch.send(embed=em), ctx="release-log")
        if err:
            await ch.send("❌ Failed to fully restore channel permissions. Staff please review logs.")

    @commands.command(name="thanos")
    async def thanos_cmd(self, ctx: commands.Context, user_id: int, depth: int = 25):
        if not self._staff_check(ctx):
            await ctx.send("❌ Staff only."); return
        try:
            depth = max(1, min(100, depth))
            to_delete: List[discord.Message] = []
            async for m in ctx.channel.history(limit=depth, oldest_first=False):
                if getattr(m.author, "id", None) == user_id:
                    to_delete.append(m)
            if to_delete:
                try:
                    await ctx.channel.delete_messages(to_delete)
                except Exception:
                    for m in to_delete:
                        try: await m.delete()
                        except Exception: pass
                await ctx.send(f"✅ Removed {len(to_delete)} messages by `{user_id}` from the last {depth}.")
            else:
                await ctx.send("ℹ️ No messages from that user in the scanned range.")
        except Exception:
            await ctx.send("❌ Failed to delete messages.")

async def setup(bot: commands.Bot):
    await bot.add_cog(Safeword(bot))

