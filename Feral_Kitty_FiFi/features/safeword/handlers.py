from __future__ import annotations
from typing import Dict
import asyncio
from datetime import datetime, timezone
import discord

from .config import sw_cfg
from .access import member_authorized, member_blocked
from .exports import export_last_messages_json
from .channel_lock import lock_channel, unlock_channel, LockSnapshot
from .constants import STAFF_FALLBACK_NAME
from ..utils.discord_resolvers import resolve_role_any
from ..utils.io_helpers import aio_retry

async def handle_safeword(bot, message: discord.Message, last_trigger_at: Dict[int, float], lock_state: Dict[int, LockSnapshot]) -> None:
    cfg = sw_cfg(bot)
    ch = message.channel
    if not isinstance(ch, discord.TextChannel): return
    if isinstance(message.author, discord.Member) and member_blocked(message.author, cfg.get("blocked_roles") or []):
        await ch.send("❌ You are not permitted to use this command."); return

    cd = int(cfg.get("cooldown_seconds") or 0)
    if cd > 0:
        last = last_trigger_at.get(ch.id, 0); now = asyncio.get_event_loop().time()
        if now - last < cd: return
        last_trigger_at[ch.id] = now

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
        log_ch = resolve_role_any(message.guild, log_chan_id)  # resolves channels too
        if isinstance(log_ch, discord.TextChannel):
            fname, blob = await export_last_messages_json(ch, history_limit)
            em = discord.Embed(
                title="Safeword Triggered",
                description=f"Channel: {ch.mention}",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            em.set_author(name=f"{message.author} • UID {message.author.id}", icon_url=message.author.display_avatar.url)
            em.add_field(name="Invoker", value=message.author.mention, inline=True)
            em.add_field(name="Jump", value=f"[Message Link]({message.jump_url})", inline=False)
            await aio_retry(lambda: log_ch.send(content="📦 Transcript attached.", embed=em, file=discord.File(blob, filename=fname)), ctx="export-log")

    err = await lock_channel(ch, cfg.get("roles_whitelist") or [STAFF_FALLBACK_NAME], lock_state)
    if err:
        await ch.send("❌ Failed to lock channel. Staff please review logs.")

async def handle_release(bot, message: discord.Message, lock_state) -> None:
    cfg = sw_cfg(bot)
    ch = message.channel
    if not isinstance(ch, discord.TextChannel) or not isinstance(message.author, discord.Member):
        return
    if not member_authorized(message.author, cfg.get("roles_whitelist") or [STAFF_FALLBACK_NAME]):
        await ch.send("❌ You do not have permission to release this channel."); return

    err = await unlock_channel(ch, lock_state)
    rel_msg = (cfg.get("release_message") or {}).get("text") or ""
    rel_img = (cfg.get("release_message") or {}).get("image_url") or ""
    embed = discord.Embed(description=rel_msg, color=discord.Color.green())
    if rel_img: embed.set_image(url=rel_img)
    await ch.send(rel_msg if not embed else None, embed=embed)

    log_chan_id = cfg.get("log_channel_id")
    if isinstance(log_chan_id, int) and log_chan_id > 0:
        log_ch = resolve_role_any(message.guild, log_chan_id)
        if isinstance(log_ch, discord.TextChannel):
            em = discord.Embed(
                title="Safeword Release",
                description=f"Channel: {ch.mention}",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            em.set_author(name=f"{message.author} • UID {message.author.id}", icon_url=message.author.display_avatar.url)
            em.add_field(name="Invoker", value=message.author.mention, inline=True)
            await aio_retry(lambda: log_ch.send(embed=em), ctx="release-log")
    if err:
        await ch.send("❌ Failed to fully restore channel permissions. Staff please review logs.")
