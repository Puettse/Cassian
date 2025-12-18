# Feral_Kitty_FiFi/features/help.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

def _embed(guild: Optional[discord.Guild]) -> discord.Embed:
    emb = discord.Embed(
        title="Command Reference",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    emb.add_field(
        name="Everyone (except `jailed`)",
        value=(
            "`!STOP!` — Lock current channel; ping configured roles; export last messages.\n"
            "`!pass <6-digit>` — Complete the welcome passphrase challenge (DM or server)."
        ),
        inline=False,
    )
    emb.add_field(
        name="Staff (whitelisted, e.g., Staff role)",
        value=(
            "`!Release` — Unlock current channel.\n"
            "`!thanos <user_id> [depth]` — Remove recent messages by user in this channel."
        ),
        inline=False,
    )
    emb.add_field(
        name="Admin",
        value=(
            "`!buildnow` — Apply renames & build roles; export snapshot.\n"
            "`!thepurge` — Delete roles listed in config.\n"
            "`!drybuild` — Plan role build (JSON).\n"
            "`!drythepurge` — Plan purge (JSON).\n"
            "`!exportroles` — Export all roles (JSON).\n"
            "`!reloadconfig` — Reload `data/config.json`.\n"
            "`!drychat [count]` — Seed N messages for testing.\n"
            "`!mypeople` — Export members to CSV.\n"
            "`!rolespanel` — Open reaction-role panel builder.\n"
            "`!rolespanel list` — List saved panels.\n"
            "`!rolespanel remove <message_id>` — Remove panel & try delete message.\n"
            "`!rolesconsole` — Member role console (add/remove/toggle).\n"
            "`!welcomepanel` — Configure welcome message, autorole, passphrase gate."
        ),
        inline=False,
    )
    emb.add_field(
        name="Config (Jonslaw)",
        value=(
            "`!jonslaw show`\n"
            "`!jonslaw setlog <#channel|id>`\n"
            "`!jonslaw setpingroles <@Role …|id …|[Name] …|clear>`\n"
            "`!jonslaw setwhitelist <@Role …|id …|[Name] …|clear>`\n"
            "`!jonslaw setlockmsg <text> [| image_url]`\n"
            "`!jonslaw setreleasemsg <text> [| image_url]`"
        ),
        inline=False,
    )
    emb.set_footer(text="You can also type $help$ or !helpmejon")
    return emb

class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="helpmejon")
    async def helpmejon(self, ctx: commands.Context):
        await ctx.send(embed=_embed(ctx.guild))

async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))

