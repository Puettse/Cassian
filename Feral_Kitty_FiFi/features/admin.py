# feral_kitty_fifi/features/admin.py
from __future__ import annotations
import io
import csv
from typing import List
from datetime import datetime, timezone
import discord
from discord.ext import commands
from ..config import load_config, save_config

class Admin(commands.Cog):
    """Admin utilities + Jonslaw config commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="reloadconfig")
    @commands.has_permissions(administrator=True)
    async def reloadconfig_cmd(self, ctx: commands.Context):
        try:
            self.bot.config = await load_config()
            await ctx.send("✅ Config reloaded.")
        except Exception:
            await ctx.send("❌ Reload failed. Check logs.")

    @commands.command(name="mypeople")
    @commands.has_permissions(administrator=True)
    async def mypeople_cmd(self, ctx: commands.Context):
        try:
            await ctx.send("⏳ Gathering members…")
            rows: List[List[str]] = [["user_id", "username", "created_at_iso", "joined_server_at_iso", "roles_csv"]]
            async with ctx.typing():
                for m in ctx.guild.members:
                    created_iso = m.created_at.replace(tzinfo=timezone.utc).isoformat() if getattr(m, "created_at", None) else ""
                    joined_iso = m.joined_at.replace(tzinfo=timezone.utc).isoformat() if getattr(m, "joined_at", None) else ""
                    roles_csv = ",".join(sorted([r.name for r in m.roles if r != ctx.guild.default_role]))
                    rows.append([str(m.id), str(m), created_iso, joined_iso, roles_csv])
            buf = io.StringIO(); csv.writer(buf).writerows(rows)
            binbuf = io.BytesIO(buf.getvalue().encode("utf-8")); binbuf.seek(0)
            fname = f"members_{ctx.guild.id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
            await ctx.send(file=discord.File(binbuf, filename=fname))
        except Exception:
            await ctx.send("❌ Export failed. Ensure **Server Members Intent** is enabled.")

    @commands.group(name="jonslaw", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def jonslaw_group(self, ctx: commands.Context):
        await ctx.send(
            "⚙️ **Jonslaw config**\n"
            "`!jonslaw show`\n"
            "`!jonslaw setlog <#channel|id>`\n"
            "`!jonslaw setpingroles <@Role …|id …|[Name] …|clear>`\n"
            "`!jonslaw setwhitelist <@Role …|id …|[Name] …|clear>`\n"
            "`!jonslaw setlockmsg <text> [| image_url]`\n"
            "`!jonslaw setreleasemsg <text> [| image_url]`"
        )

    @jonslaw_group.command(name="show")
    @commands.has_permissions(administrator=True)
    async def jonslaw_show(self, ctx: commands.Context):
        sw = (self.bot.config or {}).get("safeword", {})
        await ctx.send(
            "**Current safeword config:**\n"
            f"- log_channel_id: `{sw.get('log_channel_id')}`\n"
            f"- roles_to_ping: `{sw.get('roles_to_ping')}`\n"
            f"- roles_whitelist: `{sw.get('roles_whitelist')}`\n"
            f"- blocked_roles: `{sw.get('blocked_roles')}`\n"
            f"- lock_message: `{(sw.get('lock_message') or {}).get('text', '')[:140]}`\n"
            f"- release_message: `{(sw.get('release_message') or {}).get('text', '')[:140]}`"
        )

    @jonslaw_group.command(name="setlog")
    @commands.has_permissions(administrator=True)
    async def jonslaw_setlog(self, ctx: commands.Context, *, target: str):
        ch = None
        if target.startswith("<#") and target.endswith(">"):
            try: ch = ctx.guild.get_channel(int(target[2:-1]))
            except: ch = None
        else:
            try:
                ch = ctx.guild.get_channel(int(target))
            except:
                for c in ctx.guild.text_channels:
                    if c.name.lower() == target.lower():
                        ch = c; break
        if not isinstance(ch, discord.TextChannel):
            await ctx.send("❌ Provide a valid text channel mention or ID.")
            return
        self.bot.config.setdefault("safeword", {})["log_channel_id"] = ch.id
        await save_config(self.bot.config)
        await ctx.send(f"✅ Log channel set to {ch.mention} ({ch.id})")

    def _parse_roles(self, guild: discord.Guild, raw: str):
        out = []
        for t in raw.split():
            t = t.strip()
            if not t: continue
            if t.startswith("<@&") and t.endswith(">"):
                try: out.append(int(t[3:-1])); continue
                except: pass
            try:
                val = int(t)
                if guild.get_role(val): out.append(val); continue
            except: pass
            s = t
            if s.startswith("@") and not s.startswith("<@&"): s = s[1:]
            for rr in guild.roles:
                if rr.name.lower() == s.lower():
                    out.append(rr.id); break
        return sorted(list(set(out)))

    @jonslaw_group.command(name="setpingroles")
    @commands.has_permissions(administrator=True)
    async def jonslaw_setpingroles(self, ctx: commands.Context, *, roles: str):
        roles = roles.strip()
        if roles.lower() == "clear":
            self.bot.config.setdefault("safeword", {})["roles_to_ping"] = []
            await save_config(self.bot.config)
            await ctx.send("✅ Cleared roles_to_ping."); return
        ids = self._parse_roles(ctx.guild, roles)
        if not ids:
            await ctx.send("❌ No valid roles found."); return
        self.bot.config.setdefault("safeword", {})["roles_to_ping"] = ids
        await save_config(self.bot.config)
        await ctx.send(f"✅ roles_to_ping set to {ids}")

    @jonslaw_group.command(name="setwhitelist")
    @commands.has_permissions(administrator=True)
    async def jonslaw_setwhitelist(self, ctx: commands.Context, *, roles: str):
        roles = roles.strip()
        if roles.lower() == "clear":
            self.bot.config.setdefault("safeword", {})["roles_whitelist"] = []
            await save_config(self.bot.config)
            await ctx.send("✅ Cleared roles_whitelist."); return
        ids = self._parse_roles(ctx.guild, roles)
        if not ids:
            await ctx.send("❌ No valid roles found."); return
        self.bot.config.setdefault("safeword", {})["roles_whitelist"] = ids
        await save_config(self.bot.config)
        await ctx.send(f"✅ roles_whitelist set to {ids}")

    @jonslaw_group.command(name="setlockmsg")
    @commands.has_permissions(administrator=True)
    async def jonslaw_setlockmsg(self, ctx: commands.Context, *, text: str):
        msg, img = (text.split("|", 1) + [""])[:2] if "|" in text else (text, "")
        sw = self.bot.config.setdefault("safeword", {})
        block = sw.setdefault("lock_message", {"text": "", "image_url": ""})
        block["text"] = msg.strip(); block["image_url"] = img.strip()
        await save_config(self.bot.config)
        await ctx.send("✅ Updated lock message.")

    @jonslaw_group.command(name="setreleasemsg")
    @commands.has_permissions(administrator=True)
    async def jonslaw_setreleasemsg(self, ctx: commands.Context, *, text: str):
        msg, img = (text.split("|", 1) + [""])[:2] if "|" in text else (text, "")
        sw = self.bot.config.setdefault("safeword", {})
        block = sw.setdefault("release_message", {"text": "", "image_url": ""})
        block["text"] = msg.strip(); block["image_url"] = img.strip()
        await save_config(self.bot.config)
        await ctx.send("✅ Updated release message.")

async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))

