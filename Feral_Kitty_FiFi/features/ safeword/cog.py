from __future__ import annotations
from typing import Dict
from discord.ext import commands
import discord

from .config import sw_cfg
from .handlers import handle_safeword, handle_release
from .commands import register_commands
from .channel_lock import LockSnapshot
from ..utils.perms import staff_check_factory

class Safeword(commands.Cog):
    """Safeword handling: !STOP! / !Release, locking, pings, export, thanos."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock_state: Dict[int, LockSnapshot] = {}
        self._last_trigger_at: Dict[int, float] = {}
        self._staff_check = staff_check_factory(lambda: self.bot.config)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if getattr(message.author, "bot", False) or not message.guild:
            return
        cfg = sw_cfg(self.bot)
        if not cfg.get("enabled", True):
            return
        content = (message.content or "").strip()
        if content == (cfg.get("trigger") or "!STOP!").strip():
            await handle_safeword(self.bot, message, self._last_trigger_at, self._lock_state)
        elif content == (cfg.get("release_trigger") or "!Release").strip():
            await handle_release(self.bot, message, self._lock_state)

async def setup(bot: commands.Bot):
    cog = Safeword(bot)
    from .commands import register_commands
    register_commands(cog)
    await bot.add_cog(cog)

    # Also load slash commands for interactive setup
    from . import slash as _slash_ext
    await _slash_ext.setup(bot)



