from typing import Optional
import discord
from Feral_Kitty_FiFi.utils.io_helpers import aio_retry
from Feral_Kitty_FiFi.utils.discord_resolvers import resolve_role_any
from .constants import SAFE_CATEGORY_NAME, SAFE_LOG_CHANNEL_NAME

async def get_or_create_role(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    role = discord.utils.get(guild.roles, name=name)
    if role: return role
    if not guild.me.guild_permissions.manage_roles: return None
    try:
        return await aio_retry(lambda: guild.create_role(name=name, reason="Safeword provisioning"), ctx="create-role")
    except Exception:
        return None

async def get_or_create_safe_category(guild: discord.Guild) -> Optional[discord.CategoryChannel]:
    cat = discord.utils.get(guild.categories, name=SAFE_CATEGORY_NAME)
    if cat: return cat
    if not guild.me.guild_permissions.manage_channels: return None
    try:
        return await aio_retry(lambda: guild.create_category(SAFE_CATEGORY_NAME, reason="Safeword provisioning"), ctx="create-category")
    except Exception:
        return None

async def get_or_create_safe_channel(guild: discord.Guild, category: discord.CategoryChannel) -> Optional[discord.TextChannel]:
    chan = discord.utils.get(guild.text_channels, name=SAFE_LOG_CHANNEL_NAME)
    if chan:
        if chan.category_id != category.id and guild.me.guild_permissions.manage_channels:
            try:
                await aio_retry(lambda: chan.edit(category=category, reason="Safeword provisioning move"), ctx="move-channel")
            except Exception:
                pass
        return chan
    if not guild.me.guild_permissions.manage_channels: return None
    try:
        overwrites = { guild.default_role: discord.PermissionOverwrite(view_channel=False) }
        staff_role = resolve_role_any(guild, "Staff")
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        return await aio_retry(lambda: guild.create_text_channel(
            SAFE_LOG_CHANNEL_NAME, category=category, overwrites=overwrites, reason="Safeword provisioning"
        ), ctx="create-safeword-channel")
    except Exception:
        return None
