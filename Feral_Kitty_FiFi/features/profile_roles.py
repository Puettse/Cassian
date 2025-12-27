# Feral_Kitty_FiFi/features/profile_roles.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import discord
from discord.ext import commands

from ..config import save_config
from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any


# ---------- Defaults ----------
DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,

    # Where the public "About Me" button panel lives:
    "panel_channel_id": 1400630254585778246,

    # Where completed profiles are posted:
    "post_channel_id": 1400630394977783959,

    # Optional audit/log channel for errors/actions (can be null):
    "log_channel_id": None,

    "panel_embed": {
        "title": "Build Your Profile",
        "description": "Press **About Me** to set your profile roles and info.",
        "image_url": "",
        "color": "#5865F2",
    },

    # Role categories. Each category MUST be <= 25 roles because Discord select menus cap at 25 options.
    # You can use role IDs OR role names. IDs are best.
    "categories": [
        {
            "key": "gender",
            "label": "Gender",
            "multi": False,
            "required": True,
            "roles": [],  # e.g. [123, 456] or ["Male", "Female"]
        },
        {
            "key": "age_group",
            "label": "Age Group",
            "multi": False,
            "required": True,
            "roles": [],
        },
        {
            "key": "location",
            "label": "Location",
            "multi": False,
            "required": True,
            "roles": [],
        },
        {
            "key": "interests",
            "label": "Interests",
            "multi": True,
            "required": False,
            "roles": [],
        },
        {
            "key": "favorite_games",
            "label": "Favorite Games",
            "multi": True,
            "required": False,
            "roles": [],
        },
        {
            "key": "touch_preferences",
            "label": "Touch Preferences",
            "multi": False,
            "required": True,
            "roles": [],
        },
    ],

    # If true: when user submits, remove any roles in these categories that were previously selected but not selected now.
    "remove_unselected_in_categories": True,
}


# ---------- Small helpers ----------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hex_to_color(hex_str: str) -> discord.Color:
    s = (hex_str or "").strip().lstrip("#")
    try:
        return discord.Color(int(s, 16))
    except Exception:
        return discord.Color.blurple()


def _safe(s: Optional[str], limit: int = 1000) -> str:
    s = (s or "").strip()
    return s[:limit]


def _fmt_joined(member: discord.Member) -> str:
    if member.joined_at:
        return member.joined_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return "Unknown"


def _role_label(role: discord.Role) -> str:
    # Discord select labels max 100 chars
    return (role.name or "role")[:100]


def _chunk(text: str, limit: int = 1024) -> List[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    out = []
    i = 0
    while i < len(text):
        out.append(text[i : i + limit])
        i += limit
    return out


@dataclass
class DraftProfile:
    user_id: int
    created_at: datetime
    nickname: str
    dob: str
    favorites: str
    likes: str
    dislikes: str


# ---------- Persistent IDs ----------
PANEL_BTN_CUSTOM_ID = "profile_roles:about_me"


# ---------- UI: Modal ----------
class AboutMeModal(discord.ui.Modal):
    def __init__(self, cog: "ProfileRolesCog", guild_id: int, user_id: int):
        super().__init__(title="About Me", timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id

        # Modal limitation: max 5 text inputs.
        self.nickname = discord.ui.TextInput(
            label="Nickname (display name you want)",
            placeholder="Your nickname…",
            required=True,
            max_length=60,
        )
        self.dob = discord.ui.TextInput(
            label="DOB (optional, YYYY-MM-DD)",
            placeholder="2000-01-23",
            required=False,
            max_length=10,
        )
        self.favorites = discord.ui.TextInput(
            label="Favorites (Color / Music / Food)",
            placeholder="Color: … | Music: … | Food: …",
            style=discord.TextStyle.long,
            required=False,
            max_length=300,
        )
        self.likes = discord.ui.TextInput(
            label="Likes",
            placeholder="Things you like…",
            style=discord.TextStyle.long,
            required=False,
            max_length=500,
        )
        self.dislikes = discord.ui.TextInput(
            label="Dislikes",
            placeholder="Things you dislike…",
            style=discord.TextStyle.long,
            required=False,
            max_length=500,
        )

        for item in (self.nickname, self.dob, self.favorites, self.likes, self.dislikes):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild or self.cog.bot.get_guild(self.guild_id)
        if not guild:
            return await interaction.response.send_message("❌ Guild context missing.", ephemeral=True)

        member = guild.get_member(self.user_id)
        if not isinstance(member, discord.Member):
            try:
                member = await guild.fetch_member(self.user_id)
            except Exception:
                member = None
        if not member:
            return await interaction.response.send_message("❌ Member not found.", ephemeral=True)

        draft = DraftProfile(
            user_id=member.id,
            created_at=utcnow(),
            nickname=_safe(self.nickname.value, 60),
            dob=_safe(self.dob.value, 20),
            favorites=_safe(self.favorites.value, 300),
            likes=_safe(self.likes.value, 500),
            dislikes=_safe(self.dislikes.value, 500),
        )
        self.cog._drafts[member.id] = draft

        view = ProfileSelectView(self.cog, guild_id=guild.id, user_id=member.id)
        await interaction.response.send_message(
            "✅ Text saved. Now choose your roles below, then press **Submit Profile**.",
            view=view,
            ephemeral=True,
        )


# ---------- UI: Category Select ----------
class CategorySelect(discord.ui.Select):
    def __init__(self, cog: "ProfileRolesCog", guild: discord.Guild, category: Dict[str, Any]):
        self.cog = cog
        self.guild = guild
        self.category = category

        roles = cog._resolve_category_roles(guild, category)
        opts: List[discord.SelectOption] = []
        for r in roles[:25]:
            opts.append(discord.SelectOption(label=_role_label(r), value=str(r.id)))

        placeholder = f"{category.get('label','Select')} ({'multi' if category.get('multi') else 'one'})"
        super().__init__(
            placeholder=placeholder[:100],
            min_values=0 if not category.get("required") else 1,
            max_values=min(len(opts), 25) if category.get("multi") else (1 if opts else 1),
            options=opts,
            disabled=(len(opts) == 0),
        )

    async def callback(self, interaction: discord.Interaction):
        # Store selection into the view state (user-specific)
        view = self.view
        if not isinstance(view, ProfileSelectView):
            return
        view.selected[self.category["key"]] = [int(v) for v in self.values]
        await interaction.response.send_message(
            f"✅ Saved: **{self.category.get('label', self.category['key'])}**",
            ephemeral=True,
        )


# ---------- UI: Selection View ----------
class ProfileSelectView(discord.ui.View):
    def __init__(self, cog: "ProfileRolesCog", guild_id: int, user_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.selected: Dict[str, List[int]] = {}

        guild = cog.bot.get_guild(guild_id)
        if guild:
            cfg = cog.cfg()
            for cat in (cfg.get("categories") or []):
                if not cat.get("key"):
                    continue
                self.add_item(CategorySelect(cog, guild, cat))

    @discord.ui.button(label="Submit Profile", style=discord.ButtonStyle.success, emoji="✅")
    async def submit_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This isn’t your profile session.", ephemeral=True)

        ok, msg = await self.cog._finalize_profile(interaction.user, self.selected)
        await interaction.response.send_message(msg, ephemeral=True)

        # Disable UI after submit
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This isn’t your session.", ephemeral=True)

        self.cog._drafts.pop(self.user_id, None)
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass


# ---------- Persistent panel view ----------
class AboutMePanelView(discord.ui.View):
    def __init__(self, cog: "ProfileRolesCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="About Me",
        style=discord.ButtonStyle.primary,
        custom_id=PANEL_BTN_CUSTOM_ID,
        emoji="👤",
    )
    async def about_me_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)

        cfg = self.cog.cfg()
        if not cfg.get("enabled", True):
            return await interaction.response.send_message("This feature is disabled.", ephemeral=True)

        await interaction.response.send_modal(AboutMeModal(self.cog, interaction.guild.id, interaction.user.id))


# ---------- Cog ----------
class ProfileRolesCog(commands.Cog):
    """
    Profile builder:
      Panel -> Modal (text) -> Select menus (roles) -> Assign roles -> Post profile embed
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._drafts: Dict[int, DraftProfile] = {}  # user_id -> DraftProfile
        bot.add_view(AboutMePanelView(self))  # persistent button

    def cfg(self) -> Dict[str, Any]:
        root = self.bot.config.setdefault("profile_roles", {})
        # merge defaults without clobbering existing config
        for k, v in DEFAULT_CFG.items():
            if k not in root:
                root[k] = v if not isinstance(v, (dict, list)) else (v.copy() if isinstance(v, dict) else list(v))
        # ensure nested defaults
        root.setdefault("panel_embed", DEFAULT_CFG["panel_embed"].copy())
        root.setdefault("categories", list(DEFAULT_CFG["categories"]))
        root.setdefault("remove_unselected_in_categories", True)
        return root

    async def _log(self, guild: discord.Guild, text: str):
        cfg = self.cfg()
        log_id = cfg.get("log_channel_id")
        if not log_id:
            return
        ch = resolve_channel_any(guild, log_id)
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass

    def _resolve_category_roles(self, guild: discord.Guild, category: Dict[str, Any]) -> List[discord.Role]:
        out: List[discord.Role] = []
        for item in (category.get("roles") or []):
            role: Optional[discord.Role] = None
            if isinstance(item, int):
                role = guild.get_role(item)
            elif isinstance(item, str):
                # allow IDs in strings too
                s = item.strip()
                if s.isdigit():
                    role = guild.get_role(int(s))
                if not role:
                    role = resolve_role_any(guild, s)
            if role and role not in out:
                out.append(role)
        return out

    def _all_profile_role_ids(self, guild: discord.Guild) -> Set[int]:
        cfg = self.cfg()
        all_ids: Set[int] = set()
        for cat in (cfg.get("categories") or []):
            for r in self._resolve_category_roles(guild, cat):
                all_ids.add(r.id)
        return all_ids

    async def _finalize_profile(self, member: discord.Member, selected: Dict[str, List[int]]) -> Tuple[bool, str]:
        cfg = self.cfg()
        guild = member.guild

        draft = self._drafts.get(member.id)
        if not draft:
            return False, "❌ Your session expired. Press **About Me** again."

        # Validate required categories
        for cat in (cfg.get("categories") or []):
            if cat.get("required"):
                key = cat.get("key")
                if key and not selected.get(key):
                    return False, f"❌ Missing required selection: **{cat.get('label', key)}**"

        # Determine roles to add/remove
        all_profile_role_ids = self._all_profile_role_ids(guild)
        selected_ids: Set[int] = set()
        for key, ids in selected.items():
            for rid in ids:
                if rid in all_profile_role_ids:
                    selected_ids.add(rid)

        to_add: List[discord.Role] = []
        to_remove: List[discord.Role] = []

        if cfg.get("remove_unselected_in_categories", True):
            # remove old profile roles not selected
            for r in member.roles:
                if r.id in all_profile_role_ids and r.id not in selected_ids:
                    to_remove.append(r)

        for rid in selected_ids:
            role = guild.get_role(rid)
            if role and role not in member.roles:
                to_add.append(role)

        # Apply role changes
        failed: List[str] = []
        try:
            if to_remove:
                await member.remove_roles(*to_remove, reason="Profile update (remove unselected)")
            if to_add:
                await member.add_roles(*to_add, reason="Profile update (selected roles)")
        except discord.Forbidden:
            failed.append("Missing permissions to manage one or more roles (role hierarchy).")
        except Exception as e:
            failed.append(f"Role update error: {e!r}")

        # Build profile embed
        post_ch = resolve_channel_any(guild, cfg.get("post_channel_id"))
        if not isinstance(post_ch, discord.TextChannel):
            await self._log(guild, "❌ profile_roles.post_channel_id is not a valid text channel.")
            return False, "❌ Profile saved, but posting channel is misconfigured."

        emb = discord.Embed(
            title="Member Profile",
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        emb.set_author(name=str(member), icon_url=member.display_avatar.url)
        emb.set_thumbnail(url=member.display_avatar.url)

        emb.add_field(name="Username", value=f"{member.mention}\n`{member.id}`", inline=True)
        emb.add_field(name="Nickname", value=draft.nickname or "—", inline=True)
        emb.add_field(name="Joined", value=_fmt_joined(member), inline=True)

        # Text fields
        if draft.dob:
            emb.add_field(name="DOB (optional)", value=draft.dob, inline=True)
        if draft.favorites:
            emb.add_field(name="Favorites (Color / Music / Food)", value=_safe(draft.favorites, 1024) or "—", inline=False)
        if draft.likes:
            emb.add_field(name="Likes", value=_safe(draft.likes, 1024) or "—", inline=False)
        if draft.dislikes:
            emb.add_field(name="Dislikes", value=_safe(draft.dislikes, 1024) or "—", inline=False)

        # Role selections per category
        cats = cfg.get("categories") or []
        for cat in cats:
            key = cat.get("key")
            if not key:
                continue
            ids = selected.get(key) or []
            roles = [guild.get_role(rid) for rid in ids]
            roles = [r for r in roles if r]
            if roles:
                value = " ".join(r.mention for r in roles)
            else:
                value = "—"
            emb.add_field(name=cat.get("label", key), value=value[:1024], inline=False)

        if failed:
            emb.add_field(name="Role Update Warnings", value="\n".join(failed)[:1024], inline=False)

        # Post and confirm
        try:
            msg = await post_ch.send(embed=emb)
        except discord.Forbidden:
            await self._log(guild, "❌ Cannot post profile embed (missing permissions in post channel).")
            return False, "❌ Profile saved, but I can’t post in the configured channel."
        except Exception as e:
            await self._log(guild, f"❌ Post error: {e!r}")
            return False, "❌ Profile saved, but posting failed."

        # Persist optional record (lightweight)
        profiles = self.bot.config.setdefault("profile_roles_profiles", {})
        profiles[str(member.id)] = {
            "updated_at": utcnow().isoformat(),
            "nickname": draft.nickname,
            "dob": draft.dob,
            "favorites": draft.favorites,
            "likes": draft.likes,
            "dislikes": draft.dislikes,
            "selected_role_ids": sorted(list(selected_ids)),
            "posted_message_url": msg.jump_url,
        }
        await save_config(self.bot.config)

        # Cleanup draft
        self._drafts.pop(member.id, None)

        return True, f"✅ Profile submitted! Posted here: {msg.jump_url}"

    # ---------- Admin: publish/update the panel ----------
    @commands.has_permissions(administrator=True)
    @commands.command(name="profilepanel")
    async def profilepanel_cmd(self, ctx: commands.Context):
        """
        Admin: publish/update the About Me panel in profile_roles.panel_channel_id.
        Usage: !profilepanel
        """
        cfg = self.cfg()
        ch = resolve_channel_any(ctx.guild, cfg.get("panel_channel_id"))
        if not isinstance(ch, discord.TextChannel):
            return await ctx.reply("❌ profile_roles.panel_channel_id is not set to a valid text channel.")

        pe = cfg.get("panel_embed") or {}
        emb = discord.Embed(
            title=pe.get("title") or "Build Your Profile",
            description=pe.get("description") or "Press **About Me** to set your profile.",
            color=_hex_to_color(pe.get("color") or "#5865F2"),
            timestamp=utcnow(),
        )
        if pe.get("image_url"):
            emb.set_image(url=pe["image_url"])

        view = AboutMePanelView(self)

        # Try to edit an existing panel; else post a new one
        msg_to_edit: Optional[discord.Message] = None
        try:
            async for m in ch.history(limit=50):
                if m.author.id == (ctx.bot.user.id if ctx.bot.user else 0) and m.components:
                    # find our button custom_id
                    found = False
                    for row in m.components:
                        for comp in getattr(row, "children", []):
                            if getattr(comp, "custom_id", None) == PANEL_BTN_CUSTOM_ID:
                                found = True
                                break
                        if found:
                            break
                    if found:
                        msg_to_edit = m
                        break
        except Exception:
            msg_to_edit = None

        if msg_to_edit:
            await msg_to_edit.edit(embed=emb, view=view)
            await ctx.reply(f"✅ Updated About Me panel in {ch.mention}.")
        else:
            await ch.send(embed=emb, view=view)
            await ctx.reply(f"✅ Published About Me panel in {ch.mention}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(ProfileRolesCog(bot))
