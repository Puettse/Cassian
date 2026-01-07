from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Any, List, Dict
import discord
from Feral_Kitty_FiFi.utils.io_helpers import aio_retry
from .constants import STAFF_FALLBACK_NAME
from Feral_Kitty_FiFi.utils.discord_resolvers import resolve_role_any

@dataclass
class LockSnapshot:
    prior_send_everyone: Optional[bool]
    prior_slowmode: Optional[int]

async def lock_channel(channel: discord.TextChannel, roles_whitelist: List[Any], store_snapshot: Dict[int, LockSnapshot]) -> Optional[str]:
    guild = channel.guild
    everyone = guild.default_role
    prior = channel.overwrites_for(everyone).send_messages
    prior_slow = channel.slowmode_delay
    store_snapshot[channel.id] = LockSnapshot(prior_send_everyone=prior, prior_slowmode=prior_slow)
    try:
        await aio_retry(lambda: channel.set_permissions(everyone, send_messages=False, reason="Safeword lock"), ctx="lock-deny")
        for token in roles_whitelist or [STAFF_FALLBACK_NAME]:
            role = resolve_role_any(guild, token)
            if role:
                await aio_retry(lambda r=role: channel.set_permissions(r, send_messages=True, reason="Safeword whitelist"), ctx="lock-allow")
        await aio_retry(lambda: channel.edit(slowmode_delay=1800, reason="Safeword 30m slowmode"), ctx="lock-slowmode")
        return None
    except Exception:
        return "lock-error"

async def unlock_channel(channel: discord.TextChannel, store_snapshot: Dict[int, LockSnapshot]) -> Optional[str]:
    guild = channel.guild
    everyone = guild.default_role
    snap = store_snapshot.get(channel.id, LockSnapshot(prior_send_everyone=None, prior_slowmode=0))
    try:
        await aio_retry(lambda: channel.edit(slowmode_delay=snap.prior_slowmode or 0, reason="Safeword release"), ctx="unlock-slowmode")
        await aio_retry(lambda: channel.set_permissions(everyone, overwrite=None), ctx="unlock-clear")
        store_snapshot.pop(channel.id, None)
        return None
    except Exception:
        return "unlock-error"
