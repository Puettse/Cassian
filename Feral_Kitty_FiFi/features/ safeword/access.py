import discord
from typing import Any, List
from .constants import STAFF_FALLBACK_NAME
from ..utils.discord_resolvers import normalize

def member_authorized(member: discord.Member, roles_whitelist: List[Any]) -> bool: ...
def member_blocked(member: discord.Member, blocked_roles: List[Any]) -> bool: ...

