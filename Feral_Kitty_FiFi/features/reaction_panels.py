# feral_kitty_fifi/features/reaction_panels.py
from __future__ import annotations
from typing import Any, Dict, List, Optional
import io
import asyncio
import discord
from discord.ext import commands
from ..config import save_config
from ..utils.discord_resolvers import resolve_role_any
from ..utils.perms import can_manage_role

def _get_panel_by_message(cfg: Dict[str, Any], mid: int) -> Optional[Dict[str, Any]]:
    for p in cfg.get("reaction_panels", []):
        if p.get("message_id") == mid:
            return p
    return None

def _emoji_from_token(token: str, guild: discord.Guild) -> Optional[discord.PartialEmoji | str]:
    t = token.strip()
    if t.startswith("<") and t.endswith(">"):
        try:
            return discord.PartialEmoji.from_str(t)
        except Exception:
            return None
    if t.startswith(":") and t.endswith(":") and len(t) > 2:
        name = t[1:-1]
        for e in guild.emojis:
            if e.name == name:
                return e
        return None
    return t

def _emoji_key(e: discord.PartialEmoji | str) -> str:
    return str(e)

class _RRBState:
    def __init__(self, guild_id: int, channel_id: int):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.image_url: str = ""
        self.mode_multi: bool = True
        self.pairs: list[tuple[str, int]] = []
        self.target_channel_id: Optional[int] = None
    def mode_label(self) -> str:
        return "Multi (+)" if self.mode_multi else "Exclusive (-)"
    def as_description(self, guild: discord.Guild) -> str:
        if not self.pairs: return "_No pairs yet. Click **Add Pair**._"
        lines = []
        for ek, rid in self.pairs:
            role = guild.get_role(rid)
            lines.append(f"{ek} → {role.mention if role else f'<@&{rid}>'}")
        return "\n".join(lines)

class ReactionRoleBuilderView(discord.ui.View):
    def __init__(self, ctx: commands.Context, cog: "ReactionPanels"):
        super().__init__(timeout=600)
        self.ctx = ctx
        self.cog = cog
        self.state = _RRBState(guild_id=ctx.guild.id, channel_id=ctx.channel.id)

    async def refresh_preview(self, interaction: discord.Interaction):
        guild = interaction.guild
        desc = self.state.as_description(guild)
        tgt = guild.get_channel(self.state.target_channel_id) if self.state.target_channel_id else self.ctx.channel
        emb = discord.Embed(title="Reaction-Role Panel Builder", description=desc, color=discord.Color.blurple())
        emb.add_field(name="Mode", value=self.state.mode_label(), inline=True)
        emb.add_field(name="Target Channel", value=tgt.mention if tgt else f"<#{self.state.target_channel_id}>", inline=True)
        if self.state.image_url: emb.set_image(url=self.state.image_url)
        await interaction.message.edit(embed=emb, view=self)

    @discord.ui.button(label="Add Pair", style=discord.ButtonStyle.primary)
    async def add_pair(self, interaction: discord.Interaction, _: discord.ui.Button):
        modal = discord.ui.Modal(title="Add Emoji ↔ Role")
        emoji_in = discord.ui.TextInput(label="Emoji", placeholder=":name: or <:name:id> or ❤️", max_length=128)
        role_in  = discord.ui.TextInput(label="Role",  placeholder="mention, ID, @Name, [Exact Name]", max_length=128)
        modal.add_item(emoji_in); modal.add_item(role_in)

        async def on_submit(modal_inter: discord.Interaction):
            guild = modal_inter.guild
            eobj = _emoji_from_token(str(emoji_in.value), guild)
            if not eobj: await modal_inter.response.send_message("❌ Emoji not found/usable.", ephemeral=True); return
            ek = _emoji_key(eobj)
            rtxt = str(role_in.value).strip()
            if rtxt.startswith("[") and rtxt.endswith("]"): rtxt = rtxt[1:-1].strip()
            if rtxt.startswith("@") and not rtxt.startswith("<@&"): rtxt = rtxt[1:].strip()
            role = resolve_role_any(guild, rtxt)
            if not role: await modal_inter.response.send_message("❌ Role not found.", ephemeral=True); return
            if not can_manage_role(guild, role): await modal_inter.response.send_message("❌ Bot cannot manage that role.", ephemeral=True); return
            if any(ek == existing for existing, _ in self.state.pairs):
                await modal_inter.response.send_message("⚠️ That emoji is already mapped in this panel.", ephemeral=True); return
            self.state.pairs.append((ek, role.id))
            await modal_inter.response.send_message("✅ Added.", ephemeral=True)
            await self.refresh_preview(modal_inter)
        modal.on_submit = on_submit  # type: ignore
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Set Image", style=discord.ButtonStyle.secondary)
    async def set_image(self, interaction: discord.Interaction, _: discord.ui.Button):
        modal = discord.ui.Modal(title="Set Image URL")
        url_in = discord.ui.TextInput(label="Image URL", placeholder="https://...", max_length=512)
        modal.add_item(url_in)
        async def on_submit(mi: discord.Interaction):
            self.state.image_url = str(url_in.value).strip()
            await mi.response.send_message("✅ Image set.", ephemeral=True)
            await self.refresh_preview(mi)
        modal.on_submit = on_submit  # type: ignore
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Set Channel", style=discord.ButtonStyle.secondary)
    async def set_channel(self, interaction: discord.Interaction, _: discord.ui.Button):
        modal = discord.ui.Modal(title="Set Target Channel")
        ch_in = discord.ui.TextInput(label="Channel", placeholder="#channel, id, or exact name", max_length=128)
        modal.add_item(ch_in)
        async def on_submit(mi: discord.Interaction):
            s = str(ch_in.value).strip()
            tgt = None
            if s.startswith("<#") and s.endswith(">"):
                try: tgt = interaction.guild.get_channel(int(s[2:-1]))
                except: tgt = None
            else:
                try:
                    cid = int(s); tgt = interaction.guild.get_channel(cid)
                except:
                    for ch in interaction.guild.text_channels:
                        if ch.name.lower() == s.lower():
                            tgt = ch; break
            if not isinstance(tgt, discord.TextChannel):
                await mi.response.send_message("❌ Invalid text channel.", ephemeral=True); return
            self.state.target_channel_id = tgt.id
            await mi.response.send_message(f"✅ Target channel set to {tgt.mention}.", ephemeral=True)
            await self.refresh_preview(mi)
        modal.on_submit = on_submit  # type: ignore
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Toggle Mode", style=discord.ButtonStyle.secondary)
    async def toggle_mode(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.state.mode_multi = not self.state.mode_multi
        await interaction.response.send_message(f"Mode → **{self.state.mode_label()}**", ephemeral=True)
        await self.refresh_preview(interaction)

    @discord.ui.button(label="Preview", style=discord.ButtonStyle.secondary)
    async def preview(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("🔄 Refreshed.", ephemeral=True)
        await self.refresh_preview(interaction)

    @discord.ui.button(label="Post Panel", style=discord.ButtonStyle.success)
    async def post_panel(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.state.pairs:
            await interaction.response.send_message("❌ Add at least one pair.", ephemeral=True); return
        tgt = interaction.guild.get_channel(self.state.target_channel_id) if self.state.target_channel_id else self.ctx.channel
        if not isinstance(tgt, discord.TextChannel):
            await interaction.response.send_message("❌ Target channel invalid.", ephemeral=True); return
        panel_embed = discord.Embed(color=discord.Color.blurple())
        panel_embed.title = "Choose your roles"
        panel_embed.description = self.state.as_description(interaction.guild)
        if self.state.image_url: panel_embed.set_image(url=self.state.image_url)
        panel_msg = await tgt.send(embed=panel_embed)
        for ek, _ in self.state.pairs:
            try:
                emoji_obj = discord.PartialEmoji.from_str(ek) if ek.startswith("<") else ek
                await panel_msg.add_reaction(emoji_obj)
            except Exception:
                pass
            await asyncio.sleep(0.25)
        panel = {
            "guild_id": interaction.guild.id,
            "channel_id": panel_msg.channel.id,
            "message_id": panel_msg.id,
            "mode": "multi" if self.state.mode_multi else "exclusive",
            "mapping": {ek: rid for ek, rid in self.state.pairs}
        }
        self.cog.bot.config.setdefault("reaction_panels", []).append(panel)
        await save_config(self.cog.bot.config)
        await interaction.response.send_message(f"✅ Posted panel in {tgt.mention}.", ephemeral=True)
        await interaction.message.edit(view=None); self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("❌ Cancelled.", ephemeral=True)
        await interaction.message.edit(view=None); self.stop()

class ReactionPanels(commands.Cog):
    """Reaction-role panels and handlers."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.group(name="rolespanel", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def rolespanel_group(self, ctx: commands.Context):
        view = ReactionRoleBuilderView(ctx, self)
        emb = discord.Embed(
            title="Reaction-Role Panel Builder",
            description="_No pairs yet. Click **Add Pair**._",
            color=discord.Color.blurple(),
        )
        emb.add_field(name="Mode", value=view.state.mode_label(), inline=True)
        emb.add_field(name="Target Channel", value=ctx.channel.mention, inline=True)
        await ctx.send(embed=emb, view=view)

    @rolespanel_group.command(name="list")
    @commands.has_permissions(administrator=True)
    async def rolespanel_list(self, ctx: commands.Context):
        panels = [p for p in self.bot.config.get("reaction_panels", []) if p.get("guild_id") == ctx.guild.id]
        if not panels:
            await ctx.send("ℹ️ No reaction panels saved."); return
        lines: List[str] = []
        for p in panels:
            ch = ctx.guild.get_channel(p.get("channel_id"))
            mode = p.get("mode")
            count = len(p.get("mapping", {}))
            lines.append(f"- **mid** `{p.get('message_id')}` • **channel** {ch.mention if ch else p.get('channel_id')} • **mode** `{mode}` • **pairs** `{count}`")
        text = "\n".join(lines)
        if len(text) < 1900:
            await ctx.send(f"**Panels ({len(panels)}):**\n{text}")
        else:
            buf = text.encode("utf-8")
            await ctx.send(content=f"**Panels ({len(panels)}):**", file=discord.File(io.BytesIO(buf), filename=f"reaction_panels_{ctx.guild.id}.txt"))

    @rolespanel_group.command(name="remove")
    @commands.has_permissions(administrator=True)
    async def rolespanel_remove(self, ctx: commands.Context, message_id: int):
        panels = self.bot.config.get("reaction_panels", [])
        idx = None
        for i, p in enumerate(panels):
            if p.get("guild_id") == ctx.guild.id and p.get("message_id") == message_id:
                idx = i; break
        if idx is None:
            await ctx.send("❌ Panel not found for this guild (check the message ID).")
            return
        panel = panels.pop(idx)
        await save_config(self.bot.config)
        deleted = False
        try:
            ch = ctx.guild.get_channel(panel.get("channel_id"))
            if isinstance(ch, discord.TextChannel):
                msg = await ch.fetch_message(panel.get("message_id"))
                await msg.delete()
                deleted = True
        except Exception:
            pass
        await ctx.send(f"✅ Removed panel `{message_id}` from config." + (" Message deleted." if deleted else " (Message not deleted or not found.)"))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        try:
            if payload.user_id == (self.bot.user.id if self.bot.user else 0):
                return
            panel = _get_panel_by_message(self.bot.config, payload.message_id)
            if not panel:
                return
            key = str(payload.emoji)
            role_id = panel["mapping"].get(key)
            if not role_id:
                return
            guild = self.bot.get_guild(payload.guild_id)
            if not guild:
                return
            member = guild.get_member(payload.user_id)
            if not member or member.bot:
                return
            role = guild.get_role(role_id)
            if not role or not can_manage_role(guild, role):
                return
            if panel.get("mode") == "exclusive":
                to_remove_role_ids = [rid for k, rid in panel["mapping"].items() if k != key]
                for rid in to_remove_role_ids:
                    r = guild.get_role(rid)
                    if r and r in member.roles:
                        try:
                            await member.remove_roles(r, reason="Reaction roles (exclusive swap)")
                        except Exception:
                            pass
                channel = guild.get_channel(panel["channel_id"])
                if isinstance(channel, discord.TextChannel):
                    try:
                        msg = await channel.fetch_message(panel["message_id"])
                        for k in panel["mapping"].keys():
                            if k == key: continue
                            try:
                                emoji_obj = discord.PartialEmoji.from_str(k) if k.startswith("<") else k
                                await msg.remove_reaction(emoji_obj, member)
                            except Exception:
                                pass
                    except Exception:
                        pass
            if role not in member.roles:
                try:
                    await member.add_roles(role, reason="Reaction role assign")
                except Exception:
                    pass
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        try:
            panel = _get_panel_by_message(self.bot.config, payload.message_id)
            if not panel:
                return
            key = str(payload.emoji)
            role_id = panel["mapping"].get(key)
            if not role_id:
                return
            guild = self.bot.get_guild(payload.guild_id)
            if not guild:
                return
            member = guild.get_member(payload.user_id)
            if not member:
                try:
                    member = await guild.fetch_member(payload.user_id)
                except Exception:
                    return
            role = guild.get_role(role_id)
            if not role:
                return
            if role in member.roles:
                try:
                    await member.remove_roles(role, reason="Reaction role remove")
                except Exception:
                    pass
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionPanels(bot))

