
# feral_kitty_fifi/features/member_console.py
from __future__ import annotations
import re
import discord
from discord.ext import commands
from ..utils.discord_resolvers import resolve_member_any, resolve_role_any
from ..utils.perms import can_manage_role

class _MRCState:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.target_member_id = None
    def member(self, guild: discord.Guild):
        return guild.get_member(self.target_member_id) if self.target_member_id else None

class MemberRoleConsoleView(discord.ui.View):
    def __init__(self, ctx: commands.Context):
        super().__init__(timeout=600); self.ctx = ctx
        self.state = _MRCState(guild_id=ctx.guild.id)

    def _embed(self, guild: discord.Guild) -> discord.Embed:
        m = self.state.member(guild)
        emb = discord.Embed(title="Member Role Console", description="Set a member, then add/remove/toggle roles.", color=discord.Color.gold())
        if m:
            roles = [r.mention for r in m.roles if r != guild.default_role]
            emb.add_field(name="Target", value=f"{m.mention} (`{m.id}`)", inline=False)
            emb.add_field(name="Current Roles", value=(",".join(roles) if roles else "_None_"), inline=False)
        else:
            emb.add_field(name="Target", value="_Not set_", inline=False)
        return emb

    async def refresh(self, interaction: discord.Interaction):
        emb = self._embed(interaction.guild)
        await interaction.message.edit(embed=emb, view=self)

    @discord.ui.button(label="Set Member", style=discord.ButtonStyle.primary)
    async def set_member(self, interaction: discord.Interaction, _: discord.ui.Button):
        modal = discord.ui.Modal(title="Set Target Member")
        member_in = discord.ui.TextInput(label="Member", placeholder="@mention, ID, or exact name", max_length=128)
        modal.add_item(member_in)
        async def on_submit(mi: discord.Interaction):
            m = resolve_member_any(mi.guild, str(member_in.value))
            if not isinstance(m, discord.Member):
                await mi.response.send_message("❌ Member not found.", ephemeral=True); return
            self.state.target_member_id = m.id
            await mi.response.send_message(f"✅ Target set to {m.mention}", ephemeral=True)
            await self.refresh(mi)
        modal.on_submit = on_submit  # type: ignore
        await interaction.response.send_modal(modal)

    async def _apply_roles(self, interaction: discord.Interaction, *, add: bool, raw: str):
        m = self.state.member(interaction.guild)
        if not m:
            await interaction.response.send_message("❌ Set a target member first.", ephemeral=True); return
        tokens = re.split(r"[,\s]+", (raw or "").strip())
        tokens = [t for t in tokens if t]
        if not tokens:
            await interaction.response.send_message("❌ Provide at least one role.", ephemeral=True); return
        successes, fails = [], []
        for t in tokens:
            role = resolve_role_any(interaction.guild, t)
            if not role:
                fails.append((t, "not found")); continue
            if not can_manage_role(interaction.guild, role):
                fails.append((role.name, "cannot manage")); continue
            try:
                if add:
                    if role not in m.roles:
                        await m.add_roles(role, reason="MRC add")
                    successes.append(f"+ {role.name}")
                else:
                    if role in m.roles:
                        await m.remove_roles(role, reason="MRC remove")
                    successes.append(f"- {role.name}")
            except Exception as e:
                fails.append((role.name, type(e).__name__))
        msg = ""
        if successes: msg += "✅ " + ", ".join(successes) + "\n"
        if fails: msg += "❌ " + ", ".join([f"{n} ({why})" for n, why in fails])
        if not msg: msg = "ℹ️ No changes."
        await interaction.response.send_message(msg, ephemeral=True)
        await self.refresh(interaction)

    @discord.ui.button(label="Add Roles", style=discord.ButtonStyle.success)
    async def add_roles(self, interaction: discord.Interaction, _: discord.ui.Button):
        modal = discord.ui.Modal(title="Add Roles")
        roles_in = discord.ui.TextInput(label="Roles", placeholder="space/comma-separated: @Role, [Name], ID, Name", max_length=400)
        modal.add_item(roles_in)
        async def on_submit(mi: discord.Interaction):
            await self._apply_roles(mi, add=True, raw=str(roles_in.value))
        modal.on_submit = on_submit  # type: ignore
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Remove Roles", style=discord.ButtonStyle.danger)
    async def remove_roles(self, interaction: discord.Interaction, _: discord.ui.Button):
        modal = discord.ui.Modal(title="Remove Roles")
        roles_in = discord.ui.TextInput(label="Roles", placeholder="space/comma-separated: @Role, [Name], ID, Name", max_length=400)
        modal.add_item(roles_in)
        async def on_submit(mi: discord.Interaction):
            await self._apply_roles(mi, add=False, raw=str(roles_in.value))
        modal.on_submit = on_submit  # type: ignore
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Toggle Role", style=discord.ButtonStyle.secondary)
    async def toggle_role(self, interaction: discord.Interaction, _: discord.ui.Button):
        modal = discord.ui.Modal(title="Toggle One Role")
        role_in = discord.ui.TextInput(label="Role", placeholder="@Role, [Name], ID, Name", max_length=200)
        modal.add_item(role_in)
        async def on_submit(mi: discord.Interaction):
            m = self.state.member(mi.guild)
            if not m:
                await mi.response.send_message("❌ Set a target member first.", ephemeral=True); return
            role = resolve_role_any(mi.guild, str(role_in.value))
            if not role:
                await mi.response.send_message("❌ Role not found.", ephemeral=True); return
            if not can_manage_role(mi.guild, role):
                await mi.response.send_message("❌ Bot cannot manage that role.", ephemeral=True); return
            try:
                if role in m.roles:
                    await m.remove_roles(role, reason="MRC toggle")
                    await mi.response.send_message(f"➖ Removed {role.mention}", ephemeral=True)
                else:
                    await m.add_roles(role, reason="MRC toggle")
                    await mi.response.send_message(f"➕ Added {role.mention}", ephemeral=True)
                await self.refresh(mi)
            except Exception:
                await mi.response.send_message("❌ Failed.", ephemeral=True)
        modal.on_submit = on_submit  # type: ignore
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("🔄", ephemeral=True)
        await self.refresh(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("👋 Closed.", ephemeral=True)
        await interaction.message.edit(view=None)
        self.stop()

class MemberConsole(commands.Cog):
    """Admin-only member role console."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="rolesconsole")
    @commands.has_permissions(administrator=True)
    async def rolesconsole_cmd(self, ctx: commands.Context):
        view = MemberRoleConsoleView(ctx)
        emb = view._embed(ctx.guild)
        await ctx.send(embed=emb, view=view)

async def setup(bot: commands.Bot):
    await bot.add_cog(MemberConsole(bot))
