from typing import Any, List
import discord
from .constants import STAFF_FALLBACK_NAME
from Feral_Kitty_FiFi.utils.discord_resolvers import normalize

def member_authorized(member: discord.Member, roles_whitelist: List[Any]) -> bool:
    if not roles_whitelist:
        roles_whitelist = [STAFF_FALLBACK_NAME]
    ids = {rid for rid in roles_whitelist if isinstance(rid, int)}
    names = {normalize(rn) for rn in roles_whitelist if isinstance(rn, str)}
    return any((r.id in ids) or (normalize(r.name) in names) for r in member.roles)

def member_blocked(member: discord.Member, blocked_roles: List[Any]) -> bool:
    if not blocked_roles:
        return False
    ids = {rid for rid in blocked_roles if isinstance(rid, int)}
    names = {normalize(rn) for rn in blocked_roles if isinstance(rn, str)}
    return any((r.id in ids) or (normalize(r.name) in names) for r in member.roles)
