# feral_kitty_fifi/features/help.py
from __future__ import annotations
from typing import Optional
import discord
from discord.ext import commands

def build_help_embed(guild: Optional[discord.Guild]) -> discord.Embed:
    emb = discord.Embed(title="Command Reference", color=discord.Color.blurple())
    emb.add_field(
        name="Everyone (except `jailed`)",
        value="`!STOP!` — Lock current channel; ping configured roles; export last messages.\n"
              "`React on panels` — Get/lose roles from reaction-role panels.",
        inline=False,
    )
    emb.add_field(
        name="Staff (whitelisted, e.g., Staff role)",
        value="`!Release` — Unlock current channel.\n"
              "`!thanos <user_id> [depth]` — Remove recent messages by user in this channel.",
        inline=False,
    )
    emb.add_field(
        name="Admin",
        value="`!buildnow` — Apply renames & build roles; export snapshot.\n"
              "`!drybuild` — Plan role changes (JSON).\n"
              "`!thepurge` — Delete roles listed in config.\n"
              "`!drythepurge` — Plan purge (JSON).\n"
              "`!exportroles` — Export all roles (JSON).\n"
              "`!reloadconfig` — Reload `config.json`.\n"
              "`!drychat [count]` — Seed N messages for testing.\n"
              "`!mypeople` — Export members to CSV.\n"
              "`!rolespanel` — Open reaction-role panel builder.\n"
              "`!rolespanel list` — List saved panels.\n"
              "`!rolespanel remove <message_id>` — Remove panel & try delete message.\n"
              "`!rolesconsole` — Open member role console (add/remove/toggle).",
        inline=False,
    )
    emb.add_field(
        name="Config (Jonslaw)",
        value="`!jonslaw show`\n"
              "`!jonslaw setlog <#channel|id>`\n"
              "`!jonslaw setpingroles <@Role …|id …|[Name] …|clear>`\n"
              "`!jonslaw setwhitelist <@Role …|id …|[Name] …|clear>`\n"
              "`!jonslaw setlockmsg <text> [| image_url]`\n"
              "`!jonslaw setreleasemsg <text> [| image_url]`",
        inline=False,
    )
    emb.set_footer(text="You can also type $help$ or !helpmejon")
    return emb

class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="helpmejon")
    async def helpmejon_cmd(self, ctx: commands.Context):
        await ctx.send(embed=build_help_embed(ctx.guild))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            if message.author.bot:
                return
            if message.content.strip().lower() == "$help$":
                await message.channel.send(embed=build_help_embed(message.guild))
                return
        except Exception:
            pass
        # DO NOT call process_commands here; Safeword listener already does it.

async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))

