from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands

from .ui import SafewordConfigView
from .config import sw_cfg
from ..utils.perms import staff_check_factory

class SafewordSlash(commands.Cog):
    """Slash commands for Safeword setup."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._staff_check = staff_check_factory(lambda: self.bot.config)

    @app_commands.command(name="safeword_setup", description="Private setup for safeword roles/channels/messages.")
    async def safeword_setup(self, interaction: discord.Interaction):
        # Allow only members with Manage Guild OR staff checker
        if not interaction.user or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Server only.", ephemeral=True)
        is_staff = False
        try:
            # Prefer your existing staff checker if compatible
            dummy_ctx = type("Ctx", (), {"author": interaction.user, "guild": interaction.guild, "channel": interaction.channel})
            is_staff = bool(self._staff_check(dummy_ctx))  # may raise, so wrapped
        except Exception:
            is_staff = False
        has_manage_guild = interaction.user.guild_permissions.manage_guild

        if not (is_staff or has_manage_guild):
            return await interaction.response.send_message("❌ You need **Manage Server** or Staff permission.", ephemeral=True)

        # Seed with current config (so defaults show in modal)
        cfg = sw_cfg(self.bot)
        view = SafewordConfigView(bot=self.bot, seed=dict(cfg))
        await interaction.response.send_message("Safeword setup (private):", view=view, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(SafewordSlash(bot))
    # Best-effort sync so the slash command appears without manual sync
    try:
        await bot.tree.sync()
    except Exception:
        pass
