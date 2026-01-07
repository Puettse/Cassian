from __future__ import annotations
from typing import List
from discord.ext import commands
import discord

from .provision import get_or_create_safe_category, get_or_create_safe_channel, get_or_create_role
from .config import update_runtime_config
from .constants import SAFE_CATEGORY_NAME, SAFE_LOG_CHANNEL_NAME, SAFE_RESPONDERS_ROLE
from ..utils.perms import staff_check_factory
from ..utils.io_helpers import aio_retry
from ..utils.discord_resolvers import resolve_role_any

def register_commands(cog):
    staff_check = staff_check_factory(lambda: cog.bot.config)

    @cog.command(name="thanos")
    async def thanos_cmd(ctx: commands.Context, user_id: int, depth: int = 25):
        if not staff_check(ctx):
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

    @cog.command(name="dropit")
    async def dropit_cmd(ctx: commands.Context):
        """Provision SAFE category, SAFEWORD channel, responders role, and wire config."""
        if not staff_check(ctx):
            await ctx.send("❌ Staff only."); return
        guild = ctx.guild
        if not guild:
            await ctx.send("❌ Must be used in a server."); return

        category = await get_or_create_safe_category(guild)
        if not category:
            await ctx.send("❌ Missing `Manage Channels` permission to create the SAFE category."); return

        safe_ch = await get_or_create_safe_channel(guild, category)
        if not safe_ch:
            await ctx.send("❌ Missing `Manage Channels` permission to create the SAFEWORD channel."); return

        responders = await get_or_create_role(guild, SAFE_RESPONDERS_ROLE)
        if responders:
            try:
                await aio_retry(lambda: safe_ch.set_permissions(
                    responders, view_channel=True, send_messages=True, reason="Safeword provisioning (responders access)"
                ), ctx="grant-responders")
            except Exception:
                pass
        else:
            await ctx.send("⚠️ Could not create/find the responders role; ensure I have `Manage Roles`.")

        update_runtime_config(cog.bot, guild, safe_ch.id, responders)

        rtps = ", ".join([getattr(resolve_role_any(guild, t), "mention", str(t)) for t in (cog.bot.config["safeword"].get("roles_to_ping") or [])])
        await ctx.send(
            f"✅ Provisioned:\n"
            f"• Category: **{SAFE_CATEGORY_NAME}**\n"
            f"• Channel: {safe_ch.mention}\n"
            f"• Role: **{SAFE_RESPONDERS_ROLE}** (if permissions allowed)\n"
            f"• Wired logging + pings. Roles to ping: {rtps or '—'}"
        )
