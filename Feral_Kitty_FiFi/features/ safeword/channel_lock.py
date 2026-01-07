from dataclasses import dataclass
from typing import Optional, Any, List
import discord

@dataclass
class LockSnapshot:
    prior_send_everyone: Optional[bool]
    prior_slowmode: Optional[int]

async def lock_channel(channel: discord.TextChannel, roles_whitelist: List[Any], store_snapshot: dict) -> Optional[str]: ...
async def unlock_channel(channel: discord.TextChannel, store_snapshot: dict) -> Optional[str]: ...

