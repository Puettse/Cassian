from discord.ext import commands
from .handlers import handle_safeword, handle_release
from .config import sw_cfg
from .commands import register_commands
from ..utils.perms import staff_check_factory

class Safeword(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock_state = {}
        self._last_trigger_at = {}
        self._staff_check = staff_check_factory(lambda: self.bot.config)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild: return
        cfg = sw_cfg(self.bot)
        if not cfg.get("enabled", True): return
        content = message.content.strip()
        if content == (cfg.get("trigger") or "!STOP!").strip():
            await handle_safeword(self.bot, message, self._last_trigger_at)
        elif content == (cfg.get("release_trigger") or "!Release").strip():
            await handle_release(self.bot, message)

async def setup(bot):
    cog = Safeword(bot)
    register_commands(cog)
    await bot.add_cog(cog)

