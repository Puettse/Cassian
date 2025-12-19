# Feral_Kitty_FiFi/features/channel_builder.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any

# ---- Fancy name presets (sample set; tweak freely)
FANCY_PRESETS: List[str] = [
    "🎫🆃🅸🅲🅺🅴🆃🆂🎫",
    "⧉ᴀɴᴀɢʀᴀᴍ",
    "⧉𝑮𝒖𝒆𝒔𝒕·𝑽𝒐𝒊𝒄𝒆",
    "👿ᴅᴇᴠɪʟs・ᴘʟᴀʏɢʀᴏᴜɴᴅ👿",
    "📣 announcements",
    "💬 general-chat",
    "🧵 threads",
    "🎧 voice-lounge",
    "📁 archives",
]

# ---- Simple permission presets (applied to a role)
PERM_PRESETS = {
    "view-only": dict(view_channel=True, send_messages=False, add_reactions=False, create_public_threads=False, create_private_threads=False, connect=False, speak=False),
    "read-write": dict(view_channel=True, send_messages=True, add_reactions=True, create_public_threads=True, create_private_threads=True, connect=True, speak=True),
    "hidden": dict(view_channel=False, send_messages=False, connect=False, speak=False),
    "voice-guest": dict(view_channel=True, connect=True, speak=True, use_voice_activation=True),
    "voice-muted": dict(view_channel=True, connect=True, speak=False),
}

# ---- View state
@dataclass
class RoleRule:
    role_id: int
    preset: str = "read-write"  # key in PERM_PRESETS
    # Optional fine-grain overrides (None=not set, True/False=force)
    view_channel: Optional[bool] = None
    send_messages: Optional[bool] = None
    connect: Optional[bool] = None
    speak: Optional[bool] = None

@dataclass
class ChannelBuilderState:
    kind: str = "text"                     # "category" | "text" | "voice"
    raw_name: str = ""                     # before styling
    emoji_prefix: str = ""                 # optional
    style_preset: Optional[str] = None     # chosen from FANCY_PRESETS
    parent_category_id: Optional[int] = None
    nsfw: bool = False
    voice_bitrate: int = 64000             # 8-384 kbps depending on server
    voice_user_limit: int = 0              # 0=no limit
    role_rules: List[RoleRule] = field(default_factory=list)

    def pretty_name(self) -> str:
        base = self.style_preset if self.style_preset else self.raw_name
        base = (base or "").strip()
        if self.emoji_prefix:
            return f"{self.emoji_prefix.strip()} {base}" if base else self.emoji_prefix.strip()
        return base or "new-channel"

    def summary_lines(self, guild: discord.Guild) -> List[str]:
        lines = [
            f"**Type:** `{self.kind}`",
            f"**Name:** `{self.pretty_name()}`",
            f"**Parent:** `{guild.get_channel(self.parent_category_id).name}`" if self.parent_category_id and guild.get_channel(self.parent_category_id) else "**Parent:** _none_",
        ]
        if self.kind == "text":
            lines.append(f"**NSFW:** `{self.nsfw}`")
        if self.kind == "voice":
            lines.append(f"**Bitrate:** `{self.voice_bitrate}` • **User limit:** `{self.voice_user_limit or '∞'}`")
        if self.role_rules:
            lines.append("**Role rules:**")
            for rr in self.role_rules[:10]:
                r = guild.get_role(rr.role_id)
                lines.append(f"• {(r.mention if r else rr.role_id)} — preset `{rr.preset}`")
            if len(self.role_rules) > 10:
                lines.append(f"… and {len(self.role_rules) - 10} more")
        else:
            lines.append("_No explicit role rules; channel will inherit from category/everyone._")
        return lines

# ---- UI Modals
class SetNameModal(discord.ui.Modal, title="Set Channel/Category Name"):
    def __init__(self, view: "ChannelBuilderView"):
        super().__init__(timeout=180)
        self.view_ref = view
        self.name = discord.ui.TextInput(label="Name", placeholder="e.g. general, lounge, tickets", max_length=100)
        self.emoji = discord.ui.TextInput(label="Emoji prefix (optional)", required=False, max_length=20, placeholder="e.g. 💬")
        self.add_item(self.name); self.add_item(self.emoji)

    async def on_submit(self, interaction: discord.Interaction):
        self.view_ref.state.raw_name = str(self.name.value).strip()
        self.view_ref.state.emoji_prefix = str(self.emoji.value or "").strip()
        await interaction.response.send_message("✅ Name updated.", ephemeral=True)
        await self.view_ref.refresh(interaction)

class AddRoleRuleModal(discord.ui.Modal, title="Add Role Rule"):
    def __init__(self, view: "ChannelBuilderView"):
        super().__init__(timeout=180)
        self.view_ref = view
        self.role = discord.ui.TextInput(label="Role", placeholder="@Role, ID, or [Exact Name]", max_length=120)
        self.add_item(self.role)

    async def on_submit(self, interaction: discord.Interaction):
        role = resolve_role_any(interaction.guild, str(self.role.value))
        if not role:
            await interaction.response.send_message("❌ Role not found.", ephemeral=True); return
        if any(r.role_id == role.id for r in self.view_ref.state.role_rules):
            await interaction.response.send_message("⚠️ Rule for that role already exists.", ephemeral=True); return
        self.view_ref.state.role_rules.append(RoleRule(role_id=role.id))
        await interaction.response.send_message("✅ Role added. Use “Edit Role Rule” to change preset.", ephemeral=True)
        await self.view_ref.refresh(interaction)

class SetVoiceModal(discord.ui.Modal, title="Voice Settings"):
    def __init__(self, view: "ChannelBuilderView"):
        super().__init__(timeout=180)
        self.view_ref = view
        self.bitrate = discord.ui.TextInput(label="Bitrate (bps)", placeholder="64000 to 384000", default=str(view.state.voice_bitrate))
        self.limit = discord.ui.TextInput(label="User limit (0=no limit)", placeholder="0-99", default=str(view.state.voice_user_limit))
        self.add_item(self.bitrate); self.add_item(self.limit)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            br = max(8000, min(384000, int(str(self.bitrate.value).strip())))
        except ValueError:
            br = 64000
        try:
            lim = max(0, min(99, int(str(self.limit.value).strip())))
        except ValueError:
            lim = 0
        self.view_ref.state.voice_bitrate = br
        self.view_ref.state.voice_user_limit = lim
        await interaction.response.send_message("✅ Voice settings updated.", ephemeral=True)
        await self.view_ref.refresh(interaction)

# ---- UI View
class ChannelBuilderView(discord.ui.View):
    def __init__(self, ctx: commands.Context):
        super().__init__(timeout=900)
        self.ctx = ctx
        self.state = ChannelBuilderState()

    async def refresh(self, interaction: discord.Interaction):
        guild = interaction.guild
        lines = self.state.summary_lines(guild)
        emb = discord.Embed(
            title="Channel/Category Builder",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        emb.set_footer(text="Set fields, then Create.")
        await interaction.message.edit(embed=emb, view=self)

    # --- Kind toggle
    @discord.ui.button(label="Kind: Text", style=discord.ButtonStyle.primary)
    async def kind_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        kinds = ["text", "voice", "category"]
        idx = kinds.index(self.state.kind)
        self.state.kind = kinds[(idx + 1) % len(kinds)]
        button.label = f"Kind: {self.state.kind.capitalize()}"
        await interaction.response.send_message(f"Type → **{self.state.kind}**", ephemeral=True)
        await self.refresh(interaction)

    # --- Set Name
    @discord.ui.button(label="Set Name", style=discord.ButtonStyle.secondary)
    async def set_name(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(SetNameModal(self))

    # --- Style picker
    @discord.ui.select(placeholder="Style Preset (optional)", min_values=0, max_values=1,
                       options=[discord.SelectOption(label=p[:100], value=p) for p in FANCY_PRESETS])
    async def style_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.state.style_preset = select.values[0] if select.values else None
        await interaction.response.send_message("✅ Style set.", ephemeral=True)
        await self.refresh(interaction)

    # --- Parent category
    @discord.ui.button(label="Set Parent Category", style=discord.ButtonStyle.secondary)
    async def set_parent(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Reply with a **category** by mention/ID/name (or `none`) within 30s.", ephemeral=True)
        try:
            msg = await self.ctx.bot.wait_for("message", timeout=30.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True); return
        if msg.content.strip().lower() == "none":
            self.state.parent_category_id = None
        else:
            ch = resolve_channel_any(self.ctx.guild, msg.content)
            if not isinstance(ch, discord.CategoryChannel):
                await interaction.followup.send("❌ Not a category.", ephemeral=True); return
            self.state.parent_category_id = ch.id
        await interaction.followup.send("✅ Parent updated.", ephemeral=True)
        await self.refresh(interaction)

    # --- NSFW toggle (text only)
    @discord.ui.button(label="Toggle NSFW", style=discord.ButtonStyle.secondary)
    async def toggle_nsfw(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.state.kind != "text":
            await interaction.response.send_message("ℹ️ NSFW only applies to text channels.", ephemeral=True); return
        self.state.nsfw = not self.state.nsfw
        await interaction.response.send_message(f"NSFW → **{self.state.nsfw}**", ephemeral=True)
        await self.refresh(interaction)

    # --- Voice settings
    @discord.ui.button(label="Voice Settings", style=discord.ButtonStyle.secondary)
    async def voice_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.state.kind != "voice":
            await interaction.response.send_message("ℹ️ Voice settings only apply to voice channels.", ephemeral=True); return
        await interaction.response.send_modal(SetVoiceModal(self))

    # --- Add role rule
    @discord.ui.button(label="Add Role Rule", style=discord.ButtonStyle.success)
    async def add_role_rule(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AddRoleRuleModal(self))

    # --- Edit role rule (preset only, keeps UI simple)
    @discord.ui.button(label="Edit Role Rule", style=discord.ButtonStyle.secondary)
    async def edit_role_rule(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.state.role_rules:
            await interaction.response.send_message("ℹ️ No role rules yet.", ephemeral=True); return
        await interaction.response.send_message("Reply with role mention/ID/name to edit within 30s.", ephemeral=True)
        try:
            msg = await self.ctx.bot.wait_for("message", timeout=30.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True); return
        r = resolve_role_any(self.ctx.guild, msg.content)
        if not r:
            await interaction.followup.send("❌ Role not found.", ephemeral=True); return
        entry = next((x for x in self.state.role_rules if x.role_id == r.id), None)
        if not entry:
            await interaction.followup.send("❌ No rule for that role.", ephemeral=True); return
        # present a preset select as a temporary view
        class PresetSelect(discord.ui.View):
            def __init__(self, parent: ChannelBuilderView, rr: RoleRule):
                super().__init__(timeout=60)
                self.parent = parent
                self.rr = rr
                opts = [discord.SelectOption(label=k, value=k, description=", ".join([p for p,v in PERM_PRESETS[k].items() if v])[:95]) for k in PERM_PRESETS.keys()]
                self.select = discord.ui.Select(placeholder="Choose permission preset", options=opts, min_values=1, max_values=1)
                self.select.callback = self._on_select  # type: ignore
                self.add_item(self.select)
            async def _on_select(self, inter: discord.Interaction):
                val = self.select.values[0]
                self.rr.preset = val
                await inter.response.send_message(f"✅ Preset → `{val}`", ephemeral=True)
                await self.parent.refresh(inter)
                self.stop()
        await interaction.followup.send("Pick a preset:", view=PresetSelect(self, entry), ephemeral=True)

    # --- Review summary
    @discord.ui.button(label="Review", style=discord.ButtonStyle.secondary)
    async def review(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("🔎 Refreshed summary.", ephemeral=True)
        await self.refresh(interaction)

    # --- Create
    @discord.ui.button(label="Create", style=discord.ButtonStyle.primary)
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button):
        guild = interaction.guild
        name = self.state.pretty_name()
        parent = guild.get_channel(self.state.parent_category_id) if self.state.parent_category_id else None
        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}

        # Build overwrites (apply to each role rule)
        for rr in self.state.role_rules:
            role = guild.get_role(rr.role_id)
            if not role:
                continue
            base = PERM_PRESETS.get(rr.preset, {})
            po = discord.PermissionOverwrite()
            for key, val in base.items():
                setattr(po, key, bool(val))
            for key in ["view_channel", "send_messages", "connect", "speak"]:
                v = getattr(rr, key)
                if v is not None:
                    setattr(po, key, v)
            overwrites[role] = po

        try:
            if self.state.kind == "category":
                created = await guild.create_category(name=name, overwrites=overwrites or None, reason="ChannelBuilder create category")
            elif self.state.kind == "text":
                created = await guild.create_text_channel(
                    name=name,
                    category=parent if isinstance(parent, discord.CategoryChannel) else None,
                    nsfw=self.state.nsfw,
                    overwrites=overwrites or None,
                    reason="ChannelBuilder create text",
                )
            else:  # voice
                created = await guild.create_voice_channel(
                    name=name,
                    category=parent if isinstance(parent, discord.CategoryChannel) else None,
                    bitrate=self.state.voice_bitrate,
                    user_limit=self.state.voice_user_limit or 0,
                    overwrites=overwrites or None,
                    reason="ChannelBuilder create voice",
                )
            link = created.mention if isinstance(created, (discord.TextChannel, discord.VoiceChannel)) else f"`{created.name}`"
            await interaction.response.send_message(f"✅ Created {self.state.kind}: {link}", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Missing permissions to create channel/category or set overwrites.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Discord error: {e}", ephemeral=True)
        except Exception:
            await interaction.response.send_message("❌ Failed to create.", ephemeral=True)

    # --- Close
    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("👋 Closed.", ephemeral=True)
        await interaction.message.edit(view=None)
        self.stop()


class ChannelBuilderCog(commands.Cog):
    """Interactive console to create categories or channels with styled names and role permissions."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.has_permissions(administrator=True)
    @commands.command(name="channelpanel")
    async def channelpanel(self, ctx: commands.Context):
        view = ChannelBuilderView(ctx)
        emb = discord.Embed(
            title="Channel/Category Builder",
            description="Start by setting **Kind**, **Name/Style**, and optional **Parent**.\nAdd **Role Rules** if needed, then **Create**.",
            color=discord.Color.blurple(),
        )
        emb.add_field(name="Quick Tips", value="- Use emoji in the name.\n- Pick a style preset.\n- Add role rules for private areas.", inline=False)
        await ctx.send(embed=emb, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelBuilderCog(bot))
