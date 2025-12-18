# Feral_Kitty_FiFi/features/reminders.py
from __future__ import annotations
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

import discord
from discord.ext import commands, tasks

# WHY: keep docstring to show purpose and quick usage
"""
Reminder Cog (example)
Commands (admins can change to suit your permission model):
  !remindme <minutes> <message>    — DM yourself later
  !remindhere <minutes> <message>  — post later in this channel
"""

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class Reminders(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending: List[Dict[str, Any]] = []  # in-memory demo; swap to JSON/db later
        self._tick.start()  # background loop

    def cog_unload(self):
        self._tick.cancel()

    @tasks.loop(seconds=5)
    async def _tick(self):
        now = utcnow()
        ready, keep = [], []
        for item in self._pending:
            if item["when"] <= now:
                ready.append(item)
            else:
                keep.append(item)
        self._pending = keep
        for r in ready:
            try:
                if r["where"] == "dm":
                    user = self.bot.get_user(r["user_id"])
                    if user:
                        await user.send(f"⏰ Reminder: {r['text']}")
                else:
                    ch = self.bot.get_channel(r["channel_id"])
                    if isinstance(ch, discord.TextChannel):
                        await ch.send(f"⏰ <@{r['user_id']}> Reminder: {r['text']}")
            except Exception:
                # minimal: avoid crashing the loop
                pass

    @_tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    @commands.command(name="remindme")
    async def remind_me(self, ctx: commands.Context, minutes: int, *, text: str):
        minutes = max(1, min(60*24*14, minutes))  # cap at 14 days for demo
        self._pending.append({
            "user_id": ctx.author.id,
            "where": "dm",
            "text": text.strip(),
            "when": utcnow() + timedelta(minutes=minutes),
        })
        await ctx.send(f"✅ I’ll DM you in {minutes}m.")

    @commands.command(name="remindhere")
    async def remind_here(self, ctx: commands.Context, minutes: int, *, text: str):
        minutes = max(1, min(60*24*14, minutes))
        self._pending.append({
            "user_id": ctx.author.id,
            "where": "channel",
            "channel_id": ctx.channel.id,
            "text": text.strip(),
            "when": utcnow() + timedelta(minutes=minutes),
        })
        await ctx.send(f"✅ I’ll post here in {minutes}m.")

async def setup(bot: commands.Bot):
    await bot.add_cog(Reminders(bot))
