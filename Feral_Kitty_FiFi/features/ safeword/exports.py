import io
from typing import Tuple
import discord

async def export_last_messages_json(channel: discord.TextChannel, limit: int) -> Tuple[str, io.BytesIO]: ...

