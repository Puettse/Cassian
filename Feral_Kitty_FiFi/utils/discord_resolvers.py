from __future__ import annotations
import re
from typing import Any, List, Optional
import discord

def normalize(name: str) -> str:
    return (name or "").strip().casefold()

def find_roles_ci(guild: discord.Guild, name: str) -> List[discord.Role]:
    key = normalize(name)
    return [r for r in guild.roles if normalize(r.name) == key]

def resolve_role_any(guild: discord.Guild, token: Any) -> Optional[discord.Role]:
    if isinstance(token, int):
        return guild.get_role(token)
    if isinstance(token, str):
        t = token.strip()
        if t.startswith("<@&") and t.endswith(">"):
            try:
                return guild.get_role(int(t[3:-1]))
            except ValueError:
                return None
        try:
            rid = int(t)
            r = guild.get_role(rid)
            if r:
                return r
        except ValueError:
            pass
        if t.startswith("[") and t.endswith("]"):
            t = t[1:-1].strip()
        if t.startswith("@") and not t.startswith("<@&"):
            t = t[1:].strip()
        matches = [r for r in guild.roles if normalize(r.name) == normalize(t)]
        return matches[0] if matches else None
    return None

def resolve_channel_any(guild: discord.Guild, token: Any) -> Optional[discord.TextChannel]:
    if isinstance(token, int):
        ch = guild.get_channel(token)
        return ch if isinstance(ch, discord.TextChannel) else None
    if isinstance(token, str):
        s = token.strip()
        if s.startswith("<#") and s.endswith(">"):
            try:
                ch = guild.get_channel(int(s[2:-1]))
                return ch if isinstance(ch, discord.TextChannel) else None
            except ValueError:
                return None
        try:
            ch = guild.get_channel(int(s))
            return ch if isinstance(ch, discord.TextChannel) else None
        except ValueError:
            key = normalize(s)
            for ch in guild.text_channels:
                if normalize(ch.name) == key:
                    return ch
    return None

def resolve_member_any(guild: discord.Guild, token: str) -> Optional[discord.Member]:
    t = (token or "").strip()
    if not t:
        return None
    if t.startswith("<@") and t.endswith(">"):
        core = t.strip("<@!>")
        try:
            uid = int(core)
            m = guild.get_member(uid) or None
            if m:
                return m
        except ValueError:
            pass
    try:
        uid = int(t)
        m = guild.get_member(uid) or None
        if m:
            return m
    except ValueError:
        pass
    cand = [m for m in guild.members if normalize(str(m)) == normalize(t) or normalize(m.display_name) == normalize(t)]
    return cand[0] if cand else None
