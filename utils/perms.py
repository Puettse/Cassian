from __future__ import annotations
from typing import List, Optional, Any, Callable
import discord
from .discord_resolvers import normalize

def build_permissions(flags: List[str]) -> discord.Permissions:
    perms = discord.Permissions.none()
    valid = set(discord.Permissions.VALID_FLAGS.keys())
    for name in flags or []:
        if name in valid:
            setattr(perms, name, True)
    return perms

def parse_color(val: Any) -> discord.Color:
    if val is None: return discord.Color(0)
    if isinstance(val, int): return discord.Color(val)
    if isinstance(val, str):
        s = val.strip().lower().removeprefix("#").removeprefix("0x")
        try: return discord.Color(int(s, 16))
        except ValueError: return discord.Color(0)
    return discord.Color(0)

async def ensure_manage_roles(guild: discord.Guild) -> bool:
    me = guild.me
    return bool(me and me.guild_permissions.manage_roles)

def can_manage_role(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return bool(me and not role.managed and role < me.top_role)

def pick_manageable(guild: discord.Guild, roles: List[discord.Role]) -> Optional[discord.Role]:
    me = guild.me
    if not me: return None
    ms = [r for r in roles if not r.managed and r < me.top_role]
    ms.sort(key=lambda r: r.position)
    return ms[0] if ms else None

def staff_check_factory(get_cfg: Callable[[], dict]):
    def predicate(ctx):
        sw = (get_cfg() or {}).get("safeword", {})
        roles_whitelist = sw.get("roles_whitelist") or ["Staff"]
        if not isinstance(ctx.author, discord.Member):
            return False
        return any(
            (r.id in {rid for rid in roles_whitelist if isinstance(rid, int)}) or
            (normalize(r.name) in {normalize(n) for n in roles_whitelist if isinstance(n, str)})
            for r in ctx.author.roles
        )
    return predicate
