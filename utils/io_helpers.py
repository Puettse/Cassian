from __future__ import annotations
import io
import json
import asyncio
from typing import Any, Dict, Tuple, List
from datetime import datetime, timezone
import discord

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

async def aio_retry(coro_factory, attempts: int = 3, delay: float = 0.6, ctx: str = "retry"):
    last = None
    for i in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as e:
            last = e
            if i < attempts:
                await asyncio.sleep(delay * i)
    if last:
        raise last

def _serialize_permissions(perms: discord.Permissions) -> List[str]:
    return [f for f in discord.Permissions.VALID_FLAGS.keys() if getattr(perms, f, False)]

def _serialize_role(role: discord.Role) -> Dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "color": {"int": role.color.value, "hex": f"#{role.color.value:06X}"},
        "position": role.position,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "managed": role.managed,
        "permissions": _serialize_permissions(role.permissions),
        "created_at_iso": role.created_at.replace(tzinfo=timezone.utc).isoformat(),
    }

def export_roles_json_blob(guild: discord.Guild) -> Tuple[str, io.BytesIO]:
    roles_sorted = sorted(guild.roles, key=lambda r: r.position)
    payload = {
        "guild": {"id": guild.id, "name": guild.name},
        "exported_at_iso": now_iso(),
        "roles": [_serialize_role(r) for r in roles_sorted],
    }
    buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"roles_{guild.id}_{ts}.json"
    buf.seek(0)
    return filename, buf

def json_blob(filename_prefix: str, payload: Dict[str, Any]) -> Tuple[str, io.BytesIO]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{filename_prefix}_{ts}.json"
    buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    buf.seek(0)
    return filename, buf
