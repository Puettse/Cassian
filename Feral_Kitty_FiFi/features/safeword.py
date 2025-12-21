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

SAFE_CATEGORY_NAME = "SAFE"
SAFE_LOG_CHANNEL_NAME = "SAFEWORD"
SAFE_RESPONDERS_ROLE = "Safeword Responders"
STAFF_FALLBACK_NAME = "Staff"

class Safeword(commands.Cog):
    """Safeword handling: !STOP! / !Release, locking, pings, export, thanos."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock_state: Dict[int, LockSnapshot] = {}
        self._last_trigger_at: Dict[int, float] = {}
        self._staff_check = staff_check_factory(lambda: self.bot.config)

    def _sw_cfg(self) -> Dict[str, Any]:
        return (self.bot.config or {}).get("safeword") or {}

    def _ensure_sw_cfg(self) -> Dict[str, Any]:
        """Ensure safeword config dict exists on bot.config and return it (in-memory)."""
        if not getattr(self.bot, "config", None):
            self.bot.config = {}
        if "safeword" not in self.bot.config:
            self.bot.config["safeword"] = {}
        return self.bot.config["safeword"]

    def _member_authorized(self, member: discord.Member, guild: discord.Guild, roles_whitelist: List[Any]) -> bool:
        if not roles_whitelist: roles_whitelist = [STAFF_FALLBACK_NAME]
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

    # ---------------------------
    # Provisioning helpers
    # ---------------------------
    async def _get_or_create_role(self, guild: discord.Guild, name: str) -> Optional[discord.Role]:
        role = discord.utils.get(guild.roles, name=name)
        if role:
            return role
        if not guild.me.guild_permissions.manage_roles:
            return None
        try:
            return await aio_retry(lambda: guild.create_role(
                name=name,
                reason="Safeword provisioning"
            ), ctx="create-role")
        except Exception:
            return None

    async def _get_or_create_safe_category(self, guild: discord.Guild) -> Optional[discord.CategoryChannel]:
        cat = discord.utils.get(guild.categories, name=SAFE_CATEGORY_NAME)
        if cat:
            return cat
        if not guild.me.guild_permissions.manage_channels:
            return None
        try:
            return await aio_retry(lambda: guild.create_category(
                SAFE_CATEGORY_NAME,
                reason="Safeword provisioning"
            ), ctx="create-category")
        except Exception:
            return None

    async def _get_or_create_safe_channel(self, guild: discord.Guild, category: discord.CategoryChannel) -> Optional[discord.TextChannel]:
        chan = discord.utils.get(guild.text_channels, name=SAFE_LOG_CHANNEL_NAME)
        if chan:
            # Move under SAFE category if not already there
            if chan.category_id != category.id and guild.me.guild_permissions.manage_channels:
                try:
                    await aio_retry(lambda: chan.edit(category=category, reason="Safeword provisioning move"), ctx="move-channel")
                except Exception:
                    pass
            return chan
        if not guild.me.guild_permissions.manage_channels:
            return None
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
            }
            # best-effort grant Staff if present
            staff_role = resolve_role_any(guild, STAFF_FALLBACK_NAME)
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            return await aio_retry(lambda: guild.create_text_channel(
                SAFE_LOG_CHANNEL_NAME,
                category=category,
                overwrites=overwrites,
                reason="Safeword provisioning"
            ), ctx="create-safeword-channel")
        except Exception:
            return None

    def _update_runtime_config(self, guild: discord.Guild, log_channel_id: int, responders_role: Optional[discord.Role]):
        cfg = self._ensure_sw_cfg()
        # defaults
        cfg.setdefault("enabled", True)
        cfg.setdefault("trigger", "!STOP!")
        cfg.setdefault("release_trigger", "!Release")
        cfg.setdefault("cooldown_seconds", 0)
        cfg["log_channel_id"] = log_channel_id

        # roles to ping + whitelist
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

    # ---------------------------
    # LISTENERS
    # ---------------------------
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

        # --- LOGGING (updated to include pfp/timestamp/etc) ---
        log_chan_id = cfg.get("log_channel_id"); history_limit = int(cfg.get("history_limit") or 25)
        if isinstance(log_chan_id, int) and log_chan_id > 0:
            log_ch = resolve_channel_any(message.guild, log_chan_id)
            if isinstance(log_ch, discord.TextChannel):
                fname, blob = await self._export_last_messages_json(ch, history_limit)
                em = discord.Embed(
                    title="Safeword Triggered",
                    description=f"Channel: {ch.mention}",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                )
                em.set_author(
                    name=f"{message.author} • UID {message.author.id}",
                    icon_url=message.author.display_avatar.url
                )
                em.add_field(name="Invoker", value=message.author.mention, inline=True)
                em.add_field(name="Jump", value=f"[Message Link]({message.jump_url})", inline=False)
                await aio_retry(lambda: log_ch.send(content="📦 Transcript attached.", embed=em, file=discord.File(blob, filename=fname)), ctx="export-log")

        err = await self._lock_channel(ch, cfg.get("roles_whitelist") or [STAFF_FALLBACK_NAME])
        if err:
            await ch.send("❌ Failed to lock channel. Staff please review logs.")

    async def _handle_release(self, message: discord.Message) -> None:
        cfg = self._sw_cfg()
        ch = message.channel
        if not isinstance(ch, discord.TextChannel) or not isinstance(message.author, discord.Member):
            return
        if not self._member_authorized(message.author, message.guild, cfg.get("roles_whitelist") or [STAFF_FALLBACK_NAME]):
            await ch.send("❌ You do not have permission to release this channel.")
            return

        err = await self._unlock_channel(ch)
        rel_msg = (cfg.get("release_message") or {}).get("text") or ""
        rel_img = (cfg.get("release_message") or {}).get("image_url") or ""
        embed = discord.Embed(description=rel_msg, color=discord.Color.green())
        if rel_img: embed.set_image(url=rel_img)
        await ch.send(rel_msg if not embed else None, embed=embed)

        # --- LOGGING (updated to include pfp/timestamp) ---
        log_chan_id = cfg.get("log_channel_id")
        if isinstance(log_chan_id, int) and log_chan_id > 0:
            log_ch = resolve_channel_any(message.guild, log_chan_id)
            if isinstance(log_ch, discord.TextChannel):
                em = discord.Embed(
                    title="Safeword Release",
                    description=f"Channel: {ch.mention}",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc),
                )
                em.set_author(
                    name=f"{message.author} • UID {message.author.id}",
                    icon_url=message.author.display_avatar.url
                )
                em.add_field(name="Invoker", value=message.author.mention, inline=True)
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

    # ---------------------------
    # $dropit bootstrap command
    # ---------------------------
    @commands.command(name="dropit")
    async def dropit_cmd(self, ctx: commands.Context):
        """Provision SAFE category, SAFEWORD channel, responders role, and wire config."""
        if not self._staff_check(ctx):
            await ctx.send("❌ Staff only."); return
        guild = ctx.guild
        if not guild:
            await ctx.send("❌ Must be used in a server."); return

        # Create/get SAFE category
        category = await self._get_or_create_safe_category(guild)
        if not category:
            await ctx.send("❌ Missing `Manage Channels` permission to create the SAFE category.")
            return

        # Create/get SAFEWORD channel
        safe_ch = await self._get_or_create_safe_channel(guild, category)
        if not safe_ch:
            await ctx.send("❌ Missing `Manage Channels` permission to create the SAFEWORD channel.")
            return

        # Create/get responders role
        responders = await self._get_or_create_role(guild, SAFE_RESPONDERS_ROLE)
        if not responders:
            await ctx.send("⚠️ Could not create/find the responders role; ensure I have `Manage Roles`.")
        else:
            # Ensure channel perms: allow responders, keep @everyone hidden
            try:
                await aio_retry(lambda: safe_ch.set_permissions(
                    responders,
                    view_channel=True,
                    send_messages=True,
                    reason="Safeword provisioning (responders access)"
                ), ctx="grant-responders")
            except Exception:
                pass

        # Update in-memory config
        self._update_runtime_config(guild, safe_ch.id, responders)

        # Friendly summary
        rtps = ", ".join(
            [getattr(resolve_role_any(guild, t), "mention", str(t)) for t in (self._sw_cfg().get("roles_to_ping") or [])]
        )
        await ctx.send(
            f"✅ Provisioned:\n"
            f"• Category: **{SAFE_CATEGORY_NAME}**\n"
            f"• Channel: {safe_ch.mention}\n"
            f"• Role: **{SAFE_RESPONDERS_ROLE}** (if permissions allowed)\n"
            f"• Wired logging + pings. Roles to ping: {rtps or '—'}"
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(Safeword(bot))
