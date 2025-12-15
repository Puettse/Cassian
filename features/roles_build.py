from __future__ import annotations
from typing import Any, Dict, List, Optional
import asyncio
import discord
from discord.ext import commands
from ..config import save_config
from ..utils.discord_resolvers import normalize, find_roles_ci
from ..utils.perms import build_permissions, parse_color, ensure_manage_roles, can_manage_role, pick_manageable
from ..utils.io_helpers import export_roles_json_blob, json_blob

class RolesBuild(commands.Cog):
    """Build/rename/purge/export roles."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---- core ops (same behavior as monolith) ----
    async def rename_from_cfg(self, guild: discord.Guild, cfg: Dict[str, Any]) -> None:
        renames = (cfg or {}).get("renames") or {}
        if not renames or not await ensure_manage_roles(guild): return
        for old_name, new_name in renames.items():
            old_trim, new_trim = str(old_name).strip(), str(new_name).strip()
            if not old_trim or not new_trim: continue
            sources = find_roles_ci(guild, old_trim)
            if not sources: continue
            conflicts = find_roles_ci(guild, new_trim)
            if conflicts and all(s.id != conflicts[0].id for s in sources):
                continue
            target = pick_manageable(guild, sources)
            if not target or target.name == new_trim: continue
            try:
                await target.edit(name=new_trim, reason="Config-driven rename (no-dup)")
            except Exception:
                pass
            await asyncio.sleep(0.3)

    async def build_from_cfg(self, guild: discord.Guild, cfg: Dict[str, Any]) -> None:
        roles_cfg = (cfg or {}).get("roles") or []
        if not roles_cfg or not await ensure_manage_roles(guild): return
        for rd in roles_cfg:
            name = rd["name"].strip()
            color = parse_color(rd.get("color"))
            perms = build_permissions(rd.get("permissions", []))
            candidates = find_roles_ci(guild, name)
            if candidates:
                target = pick_manageable(guild, candidates)
                if target:
                    try:
                        await target.edit(colour=color, permissions=perms, reason="Role builder sync (no-dup)")
                    except Exception:
                        pass
            else:
                try:
                    await guild.create_role(name=name, colour=color, permissions=perms, reason="Role builder create")
                except Exception:
                    pass
            await asyncio.sleep(0.4)

    def _purge_candidates(self, guild: discord.Guild, cfg: Dict[str, Any]) -> List[discord.Role]:
        targets = {normalize(n) for n in ((cfg or {}).get("purge") or []) if isinstance(n, str) and n.strip()}
        roles = [r for r in guild.roles if normalize(r.name) in targets]
        roles.sort(key=lambda r: r.position)
        return roles

    async def purge_roles(self, guild: discord.Guild, cfg: Dict[str, Any]) -> Dict[str, List[str]]:
        report = {"deleted": [], "skipped": [], "missing": []}
        if not await ensure_manage_roles(guild):
            report["skipped"].append("Missing Manage Roles"); return report
        requested = [n for n in ((cfg or {}).get("purge") or []) if isinstance(n, str)]
        existing_norm = {normalize(r.name) for r in guild.roles}
        for name in requested:
            if normalize(name) not in existing_norm:
                report["missing"].append(name)
        for role in self._purge_candidates(guild, cfg):
            if not can_manage_role(guild, role):
                report["skipped"].append(f"skip: {role.name}"); continue
            try:
                await role.delete(reason="Config purge")
                report["deleted"].append(role.name)
            except Exception:
                report["skipped"].append(f"error: {role.name}")
            await asyncio.sleep(0.4)
        return report

    # ---- commands ----
    @commands.command(name="buildnow")
    @commands.has_permissions(administrator=True)
    async def buildnow_cmd(self, ctx: commands.Context):
        await ctx.send("🔧 Renames → Build → Export…")
        try:
            await self.rename_from_cfg(ctx.guild, self.bot.config)  # type: ignore[arg-type]
            await self.build_from_cfg(ctx.guild, self.bot.config)   # type: ignore[arg-type]
            filename, blob = export_roles_json_blob(ctx.guild)      # type: ignore[arg-type]
            await ctx.send(content="✅ Done. Snapshot:", file=discord.File(blob, filename=filename))
        except Exception:
            await ctx.send("❌ Failed.")

    @commands.command(name="thepurge")
    @commands.has_permissions(administrator=True)
    async def thepurge_cmd(self, ctx: commands.Context):
        await ctx.send("🧹 Purging roles...")
        try:
            report = await self.purge_roles(ctx.guild, self.bot.config)  # type: ignore[arg-type]
            lines = []
            if report["deleted"]: lines.append("Deleted: " + ", ".join(report["deleted"]))
            if report["skipped"]: lines.append("Skipped: " + "; ".join(report["skipped"]))
            if report["missing"]: lines.append("Missing: " + ", ".join(report["missing"]))
            await ctx.send("✅ Done.\n" + ("\n".join(lines) if lines else "Nothing to delete."))
        except Exception:
            await ctx.send("❌ Purge failed.")

    @commands.command(name="exportroles")
    @commands.has_permissions(administrator=True)
    async def exportroles_cmd(self, ctx: commands.Context):
        try:
            filename, blob = export_roles_json_blob(ctx.guild)  # type: ignore[arg-type]
            await ctx.send(content="📦 Roles export:", file=discord.File(blob, filename=filename))
        except Exception:
            await ctx.send("❌ Export failed.")

    @commands.command(name="drybuild")
    @commands.has_permissions(administrator=True)
    async def drybuild_cmd(self, ctx: commands.Context):
        try:
            plan = {"note": "attach your plan_build_actions here if desired"}
            fname, blob = json_blob(f"build_plan_{ctx.guild.id}", plan)
            await ctx.send("🛠️ Dry build ready (plan placeholder).", file=discord.File(blob, filename=fname))
        except Exception:
            await ctx.send("❌ Dry build failed.")

    @commands.command(name="drythepurge")
    @commands.has_permissions(administrator=True)
    async def drythepurge_cmd(self, ctx: commands.Context):
        try:
            plan = {"note": "attach your plan_purge_actions here if desired"}
            fname, blob = json_blob(f"purge_plan_{ctx.guild.id}", plan)
            await ctx.send("🧹 Dry purge ready (plan placeholder).", file=discord.File(blob, filename=fname))
        except Exception:
            await ctx.send("❌ Dry purge failed.")

async def setup(bot: commands.Bot):
    await bot.add_cog(RolesBuild(bot))
