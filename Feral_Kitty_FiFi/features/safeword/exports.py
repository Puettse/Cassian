from __future__ import annotations
from typing import Tuple
import io, json
from datetime import datetime, timezone
import discord
from Feral_Kitty_FiFi.utils.io_helpers import now_iso

async def export_last_messages_json(channel: discord.TextChannel, limit: int) -> Tuple[str, io.BytesIO]:
    msgs = []
    async for m in channel.history(limit=max(1, min(100, limit)), oldest_first=False):
        msgs.append({
            "id": m.id,
            "author": {"id": m.author.id, "name": f"{m.author}", "bot": bool(getattr(m.author, 'bot', False))},
            "created_at_iso": m.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "content": m.content,
            "attachments": [{"id": a.id, "filename": a.filename, "url": a.url, "size": a.size} for a in m.attachments],
            "embeds": [{"type": e.type, "title": getattr(e, 'title', None), "description": getattr(e, 'description', None)} for e in m.embeds],
            "reference": {"message_id": getattr(m.reference, 'message_id', None)} if m.reference else None,
            "jump_url": m.jump_url,
        })
    payload = {"channel": {"id": channel.id, "name": channel.name}, "exported_at_iso": now_iso(), "count": len(msgs), "messages": msgs}
    buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    fname = f"safeword_{channel.id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    buf.seek(0)
    return fname, buf
