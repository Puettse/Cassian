# Feral_Kitty_FiFi/features/welcome_gate.py
from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set

import discord
from discord.ext import commands, tasks

from ..config import save_config
from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any
from ..utils.perms import can_manage_role


# ===== utils =====
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _parse_yyyy_mm_dd(s: str) -> Optional[date]:
    try:
        y, m, d = [int(p) for p in s.strip().split("-")]
        return date(y, m, d)
    except Exception:
        return None


def _calc_age(dob: date, today: Optional[date] = None) -> int:
    today = today or utcnow().date()
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return years


def _slug_username(member: discord.Member) -> str:
    base = member.name.lower()
    base = re.sub(r"[^a-z0-9\-]+", "-", base)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    return base or "user"


# ===== data =====
@dataclass
class Challenge:
    user_id: int
    code: str
    expires_at: datetime
    attempts: int = 0
    def expired(self) -> bool: return utcnow() >= self.expires_at


DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,
    "channel_id": None,            # legacy fallback welcome channel
    "panel_channel_id": None,      # persistent panel channel
    "panel_message_id": None,      # persistent panel message id
    "dm_user": False,              # legacy; DM now happens on Verify click
    "log_channel_id": None,
    "message": {
        "title": "Welcome!",
        "description": "Welcome to the server, {mention}.\nClick **Verify** below to get started.",
        "image_url": "",
    },
    "autorole_id": None,           # role to give on join (e.g., Gated)
    "jailed_role_id": None,        # role assigned when DOB fails
    "jail_info_channel_id": None,  # channel to announce jailing requirement (optional)
    "ticket_category_id": None,    # category to create id-verify tickets under (optional)
    "ticket_prefix": "id-verify",  # ticket channel prefix
    "security_role_id": None,      # ping & grant access
    "staff_role_id": None,         # ping & grant access
    "minimum_age": 18,
    "challenge": {
        "enabled": True,
        "timeout_hours": 72,
        "max_attempts": 5,
        "remove_role_id": None,    # gated role to remove upon success
        "grant_role_id": None,     # role to grant upon success (verified)
    },
}

PERSISTENT_VERIFY_ID = "welcome_gate:verify"


# ===== persistent verify panel =====
class WelcomePanelView(discord.ui.View):
    """Persistent Verify panel (timeout=None + custom_id)."""
    def __init__(self, cog: "WelcomeGate"):
        super().__init__(timeout=None)
        self.cog = cog
        btn = discord.ui.Button(label="Verify", style=discord.ButtonStyle.primary, custom_id=PERSISTENT_VERIFY_ID)
        btn.callback = self._on_verify_clicked
        self.add_item(btn)

    async def _on_verify_clicked(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)
        await self.cog._start_verification_flow(interaction, interaction.user)


class VerifyModal(discord.ui.Modal):
    """Popup modal: DOB + 6-digit passcode."""
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int, require_code: bool):
        super().__init__(title="Verification", timeout=180)
        self.cog = cog; self.guild_id = guild_id; self.user_id = user_id; self.require_code = require_code
        self.dob = discord.ui.TextInput(label="Date of Birth (YYYY-MM-DD)", placeholder="2004-07-15", required=True, max_length=10)
        self.add_item(self.dob)
        self.code = None
        if require_code:
            self.code = discord.ui.TextInput(label="Passcode (check your DMs)", placeholder="6 digits", required=True, max_length=16)
            self.add_item(self.code)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._handle_verify_submission(
            interaction, self.guild_id, self.user_id, self.dob.value, self.code.value if self.code else None
        )


# ===== modal-only config console =====
class WelcomeConfigView(discord.ui.View):
    """Admin config console using Modals only (no chat prompts)."""
    panel_message: Optional[discord.Message] = None

    def __init__(self, cog: "WelcomeGate", ctx: commands.Context):
        super().__init__(timeout=600)
        self.cog = cog
        self.ctx = ctx

    async def render(self):
        if not self.panel_message: return
        g = self.ctx.guild; cfg = self.cog.cfg(g.id)
        def _role(rid): 
            r = resolve_role_any(g, rid); return r.mention if r else str(rid)
        def _chan(cid):
            c = resolve_channel_any(g, cid); return c.mention if isinstance(c, discord.TextChannel) else str(cid)
        def _cat(cid):
            c = resolve_channel_any(g, cid); return c.name if isinstance(c, discord.CategoryChannel) else str(cid)
        message = cfg.get("message") or {}

        emb = discord.Embed(
            title="Welcome Gate — Config",
            description="Use the buttons below. Changes save instantly.",
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        emb.add_field(name="Enabled", value=str(bool(cfg.get("enabled", True))), inline=True)
        emb.add_field(name="Minimum Age", value=str(int(cfg.get("minimum_age") or 18)), inline=True)
        emb.add_field(name="Ticket Prefix", value=str(cfg.get("ticket_prefix") or "id-verify"), inline=True)
        emb.add_field(name="Jailed Role", value=_role(cfg.get("jailed_role_id")), inline=True)
        emb.add_field(name="Security Role", value=_role(cfg.get("security_role_id")), inline=True)
        emb.add_field(name="Staff Role", value=_role(cfg.get("staff_role_id")), inline=True)
        emb.add_field(name="Ticket Category", value=_cat(cfg.get("ticket_category_id")), inline=True)
        emb.add_field(name="Jail Info Channel", value=_chan(cfg.get("jail_info_channel_id")), inline=True)
        emb.add_field(name="Panel Channel", value=_chan(cfg.get("panel_channel_id")), inline=True)
        emb.add_field(name="Message Title", value=(message.get("title") or "Welcome!"), inline=True)
        preview = (message.get("description") or "")[:180] or "_empty_"
        emb.add_field(name="Message Preview", value=preview, inline=False)
        if message.get("image_url"): emb.set_image(url=message["image_url"])
        await self.panel_message.edit(embed=emb, view=self)

    # ----- Base modal -----
    class _BaseModal(discord.ui.Modal):
        def __init__(self, view: "WelcomeConfigView", title: str):
            super().__init__(title=title, timeout=120)
            self.view = view
        async def _ok(self, interaction: discord.Interaction, msg: str):
            await save_config(self.view.cog.bot.config)
            await interaction.response.send_message(msg, ephemeral=True)
            await self.view.render()
        async def _fail(self, interaction: discord.Interaction, msg: str):
            await interaction.response.send_message(msg, ephemeral=True)

    # ----- Specific Modals -----
    class SetRoleModal(_BaseModal):
        def __init__(self, view: "WelcomeConfigView", label: str, key: str):
            super().__init__(view, f"Set {label}")
            self.key = key
            self.input = discord.ui.TextInput(
                label="Role mention / ID / [Exact Name] (or 'clear')", required=True, max_length=100
            )
            self.add_item(self.input)
        async def on_submit(self, interaction: discord.Interaction):
            g = self.view.ctx.guild; cfg = self.view.cog.cfg(g.id)
            text = self.input.value.strip()
            if text.lower() == "clear":
                cfg[self.key] = None
                return await self._ok(interaction, "✅ Cleared.")
            role = resolve_role_any(g, text)
            if not role: return await self._fail(interaction, "❌ Role not found.")
            cfg[self.key] = role.id
            await self._ok(interaction, f"✅ Set to {role.mention}.")

    class SetChannelModal(_BaseModal):
        def __init__(self, view: "WelcomeConfigView", label: str, key: str, allow_category: bool = False):
            super().__init__(view, f"Set {label}")
            self.key = key; self.allow_category = allow_category
            self.input = discord.ui.TextInput(
                label=("Category" if allow_category else "Channel") + " mention / ID / exact name (or 'clear')",
                required=True, max_length=100
            )
            self.add_item(self.input)
        async def on_submit(self, interaction: discord.Interaction):
            g = self.view.ctx.guild; cfg = self.view.cog.cfg(g.id); text = self.input.value.strip()
            if text.lower() == "clear":
                cfg[self.key] = None
                return await self._ok(interaction, "✅ Cleared.")
            ch = resolve_channel_any(g, text)
            if self.allow_category:
                if not isinstance(ch, discord.CategoryChannel): return await self._fail(interaction, "❌ Not a category.")
            else:
                if not isinstance(ch, discord.TextChannel): return await self._fail(interaction, "❌ Not a text channel.")
            cfg[self.key] = ch.id
            await self._ok(interaction, f"✅ Set to {ch.name if isinstance(ch, discord.CategoryChannel) else ch.mention}.")

    class SetMinAgeModal(_BaseModal):
        def __init__(self, view: "WelcomeConfigView"):
            super().__init__(view, "Set Minimum Age")
            self.age = discord.ui.TextInput(label="Minimum Age (e.g., 18)", required=True, max_length=3, default="18")
            self.add_item(self.age)
        async def on_submit(self, interaction: discord.Interaction):
            try:
                val = max(13, min(99, int(self.age.value.strip())))
            except ValueError:
                return await self._fail(interaction, "❌ Numbers only.")
            cfg = self.view.cog.cfg(self.view.ctx.guild.id)
            cfg["minimum_age"] = val
            await self._ok(interaction, f"✅ Minimum age set to {val}.")

    class SetTicketPrefixModal(_BaseModal):
        def __init__(self, view: "WelcomeConfigView"):
            super().__init__(view, "Set Ticket Prefix")
            self.prefix = discord.ui.TextInput(label="Ticket Prefix", required=True, max_length=24, default="id-verify")
            self.add_item(self.prefix)
        async def on_submit(self, interaction: discord.Interaction):
            val = self.prefix.value.strip().lower() or "id-verify"
            cfg = self.view.cog.cfg(self.view.ctx.guild.id)
            cfg["ticket_prefix"] = val
            await self._ok(interaction, f"✅ Ticket prefix set to `{val}`.")

    class SetMessageModal(_BaseModal):
        def __init__(self, view: "WelcomeConfigView"):
            super().__init__(view, "Set Welcome Message")
            self.title_in = discord.ui.TextInput(label="Title", required=False, max_length=256)
            self.desc_in = discord.ui.TextInput(label="Description (supports {mention})", style=discord.TextStyle.paragraph, required=False, max_length=2000)
            self.img_in = discord.ui.TextInput(label="Image URL (optional)", required=False, max_length=512)
            self.add_item(self.title_in); self.add_item(self.desc_in); self.add_item(self.img_in)
        async def on_submit(self, interaction: discord.Interaction):
            cfg = self.view.cog.cfg(self.view.ctx.guild.id)
            m = cfg.setdefault("message", {})
            if self.title_in.value is not None: m["title"] = self.title_in.value.strip()
            if self.desc_in.value is not None: m["description"] = self.desc_in.value.strip()
            if self.img_in.value is not None: m["image_url"] = self.img_in.value.strip()
            await self._ok(interaction, "✅ Message updated.")

    # ----- Buttons -> Modals / actions -----
    @discord.ui.button(label="Toggle Enabled", style=discord.ButtonStyle.primary, row=0)
    async def btn_toggle_enabled(self, interaction: discord.Interaction, _: discord.ui.Button):
        cfg = self.cog.cfg(self.ctx.guild.id); cfg["enabled"] = not bool(cfg.get("enabled", True))
        await save_config(self.cog.bot.config)
        await interaction.response.send_message(f"Enabled → {cfg['enabled']}", ephemeral=True)
        await self.render()

    @discord.ui.button(label="Set Minimum Age", style=discord.ButtonStyle.secondary, row=0)
    async def btn_min_age(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self.SetMinAgeModal(self))

    @discord.ui.button(label="Set Jailed Role", style=discord.ButtonStyle.secondary, row=1)
    async def btn_jailed(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self.SetRoleModal(self, "Jailed Role", "jailed_role_id"))

    @discord.ui.button(label="Set Security Role", style=discord.ButtonStyle.secondary, row=1)
    async def btn_security(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self.SetRoleModal(self, "Security Role", "security_role_id"))

    @discord.ui.button(label="Set Staff Role", style=discord.ButtonStyle.secondary, row=1)
    async def btn_staff(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self.SetRoleModal(self, "Staff Role", "staff_role_id"))

    @discord.ui.button(label="Set Ticket Category", style=discord.ButtonStyle.secondary, row=2)
    async def btn_ticket_cat(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self.SetChannelModal(self, "Ticket Category", "ticket_category_id", allow_category=True))

    @discord.ui.button(label="Set Jail Info Channel", style=discord.ButtonStyle.secondary, row=2)
    async def btn_jail_info(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self.SetChannelModal(self, "Jail Info Channel", "jail_info_channel_id", allow_category=False))

    @discord.ui.button(label="Set Ticket Prefix", style=discord.ButtonStyle.secondary, row=2)
    async def btn_ticket_prefix(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self.SetTicketPrefixModal(self))

    @discord.ui.button(label="Set Welcome Text/Image", style=discord.ButtonStyle.success, row=3)
    async def btn_msg(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self.SetMessageModal(self))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, row=3)
    async def btn_close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("👋 Closed.", ephemeral=True)
        if self.panel_message: await self.panel_message.edit(view=None)
        self.stop()


# ===== cog =====
class WelcomeGate(commands.Cog):
    """Permanent Verify panel + DOB/passcode modal + underage jail & ticket + modal config console."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active: Dict[int, Challenge] = {}
        self._used_codes: Set[str] = set()
        self._sweeper.start()
        self.bot.add_view(WelcomePanelView(self))  # persistent
        if not getattr(self.bot, "intents", None) or not self.bot.intents.members:
            print("[WelcomeGate] ERROR: Intents.members disabled; on_member_join won’t fire.")

    def cog_unload(self):
        self._sweeper.cancel()

    # ---- cfg ----
    def cfg(self, guild_id: int) -> Dict[str, Any]:
        all_cfg = self.bot.config.setdefault("welcome_gate", {})
        for k, v in DEFAULT_CFG.items():
            all_cfg.setdefault(k, v if not isinstance(v, dict) else v.copy())
        all_cfg.setdefault("challenge", {}).setdefault("enabled", True)
        return all_cfg

    # ---- helpers ----
    def _gen_code(self) -> str:
        for _ in range(1000):
            code = f"{random.randint(0, 999_999):06d}"
            if code not in self._used_codes and all(ch.code != code for ch in self._active.values()):
                return code
        return f"{random.randint(0, 999_999):06d}"

    async def _log(self, guild: discord.Guild, text: str):
        cfg = self.cfg(guild.id)
        ch = resolve_channel_any(guild, cfg.get("log_channel_id"))
        if isinstance(ch, discord.TextChannel):
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())

    def _format_panel_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = self.cfg(guild.id); msg = cfg.get("message") or {}
        emb = discord.Embed(
            title=(msg.get("title") or "Welcome!"),
            description=(msg.get("description") or "").replace("{mention}", "{mention}"),
            color=discord.Color.blurple(), timestamp=utcnow(),
        )
        if msg.get("image_url"): emb.set_image(url=msg["image_url"])
        emb.set_footer(text="Press Verify to continue")
        return emb

    def _format_dm_code_embed(self, member: discord.Member, code: str, hours: int) -> discord.Embed:
        return discord.Embed(
            title="Verification Code",
            description=f"Hi {member.mention},\nUse the code below in the popup and complete your DOB.\n**Code:** `{code}`\nExpires in **{hours}h**.",
            color=discord.Color.green(), timestamp=utcnow(),
        )

    def _welcome_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        cfg = self.cfg(guild.id)
        for key in ("panel_channel_id", "channel_id"):
            ch = resolve_channel_any(guild, cfg.get(key))
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
                return ch
        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            return guild.system_channel
        for ch in guild.text_channels:
            p = ch.permissions_for(guild.me)
            if p.read_messages and p.send_messages: return ch
        return None

    # ---- background ----
    @tasks.loop(minutes=5)
    async def _sweeper(self):
        expired = [uid for uid, ch in list(self._active.items()) if ch.expired()]
        for uid in expired: self._active.pop(uid, None)
        if len(self._used_codes) > 20000:
            for code in list(self._used_codes)[:5000]: self._used_codes.discard(code)

    @_sweeper.before_loop
    async def _before_sweeper(self):
        await self.bot.wait_until_ready()

    # ---- events ----
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild; cfg = self.cfg(guild.id)
        if not cfg.get("enabled", True): return
        auto = resolve_role_any(guild, cfg.get("autorole_id"))
        if auto and can_manage_role(guild, auto):
            try: await member.add_roles(auto, reason="WelcomeGate autorole")
            except Exception: pass
        # permanent welcome panel is posted via !welcomepublish

    # ---- verify flow ----
    async def _start_verification_flow(self, interaction: discord.Interaction, member: discord.Member):
        guild = member.guild; cfg = self.cfg(guild.id); chall_cfg = cfg.get("challenge") or {}
        code = None
        if chall_cfg.get("enabled", True):
            hours = clamp(int(chall_cfg.get("timeout_hours") or 72), 1, 72)
            code = self._gen_code()
            self._active[member.id] = Challenge(user_id=member.id, code=code, expires_at=utcnow() + timedelta(hours=hours))
            try: await member.send(embed=self._format_dm_code_embed(member, code, hours))
            except Exception: await self._log(guild, f"✉️ DM failed for {member.mention} during verify.")
        await interaction.response.send_modal(VerifyModal(self, guild.id, member.id, require_code=bool(code)))

    async def _handle_verify_submission(self, interaction: discord.Interaction, guild_id: int, user_id: int, dob_text: str, code_text: Optional[str]):
        guild = interaction.guild or self.bot.get_guild(guild_id)
        if not guild: return await interaction.response.send_message("❌ Guild context missing.", ephemeral=True)
        try: member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except Exception: member = None
        if not member: return await interaction.response.send_message("❌ Member not found.", ephemeral=True)

        cfg = self.cfg(guild.id); min_age = int(cfg.get("minimum_age") or 18)
        dob = _parse_yyyy_mm_dd(dob_text)
        if not dob: return await interaction.response.send_message("❌ DOB format must be YYYY-MM-DD.", ephemeral=True)
        age = _calc_age(dob)

        if age < min_age:
            await interaction.response.send_message(
                f"🚫 You must be **{min_age}+**. You have been placed in **jail** and an **ID verification ticket** was opened.",
                ephemeral=True,
            )
            await self._log(guild, f"🚨 Underage ({age}) → jailing & opening ticket for {member.mention}.")
            await self._jail_and_open_id_ticket(member)
            return

        chall_cfg = cfg.get("challenge") or {}
        if chall_cfg.get("enabled", True):
            ch = self._active.get(member.id)
            if not ch or ch.expired():
                self._active.pop(member.id, None)
                return await interaction.response.send_message("⏱️ Session expired. Click Verify again.", ephemeral=True)
            max_attempts = clamp(int(chall_cfg.get("max_attempts") or 5), 1, 10)
            if ch.attempts >= max_attempts:
                self._active.pop(member.id, None)
                return await interaction.response.send_message("❌ Attempts exceeded. Ask staff.", ephemeral=True)
            if not code_text or code_text.strip() != ch.code:
                ch.attempts += 1
                remain = max_attempts - ch.attempts
                return await interaction.response.send_message(f"❌ Incorrect passcode. Attempts left: **{remain}**.", ephemeral=True)
            gated = resolve_role_any(guild, chall_cfg.get("remove_role_id"))
            grant = resolve_role_any(guild, chall_cfg.get("grant_role_id"))
            if gated and can_manage_role(guild, gated) and gated in member.roles:
                try: await member.remove_roles(gated, reason="WelcomeGate verified - remove gated")
                except Exception: pass
            if grant and can_manage_role(guild, grant) and grant not in member.roles:
                try: await member.add_roles(grant, reason="WelcomeGate verified - grant")
                except Exception: pass
            self._used_codes.add(ch.code); self._active.pop(member.id, None)

        await interaction.response.send_message("✅ Verified. Welcome!", ephemeral=True)
        await self._log(guild, f"✅ {member.mention} verified (age {age}).")

    # ---- jailing & ticket helpers ----
    async def _jail_and_open_id_ticket(self, member: discord.Member):
        guild = member.guild; cfg = self.cfg(guild.id)
        jailed_role = resolve_role_any(guild, cfg.get("jailed_role_id"))
        if not jailed_role: return await self._log(guild, f"⚠️ jailed_role_id not set; cannot jail {member.mention}.")

        roles_to_remove = []
        for r in member.roles:
            if r.is_default(): continue
            if can_manage_role(guild, r): roles_to_remove.append(r)
        if roles_to_remove:
            try: await member.remove_roles(*roles_to_remove, reason="WelcomeGate DOB failed → jail")
            except Exception: pass

        if can_manage_role(guild, jailed_role) and jailed_role not in member.roles:
            try: await member.add_roles(jailed_role, reason="WelcomeGate DOB failed → jail")
            except Exception: pass

        channel = await self._create_id_ticket_channel(member)
        if channel: await self._post_ticket_intro(channel, member)
        else: await self._log(guild, f"⚠️ Failed to create ticket channel for {member.mention}.")
        await self._maybe_post_jail_info(member)

    async def _create_id_ticket_channel(self, member: discord.Member) -> Optional[discord.TextChannel]:
        guild = member.guild; cfg = self.cfg(guild.id)
        prefix = (cfg.get("ticket_prefix") or "id-verify").strip() or "id-verify"
        base = f"{prefix}-{_slug_username(member)}"; name = base

        category = None
        cat = resolve_channel_any(guild, cfg.get("ticket_category_id"))
        if isinstance(cat, discord.CategoryChannel): category = cat

        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)

        security = resolve_role_any(guild, cfg.get("security_role_id"))
        staff = resolve_role_any(guild, cfg.get("staff_role_id"))
        jailed_role = resolve_role_any(guild, cfg.get("jailed_role_id"))

        mod_pw = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, manage_messages=True)
        if security: overwrites[security] = mod_pw
        if staff and staff != security: overwrites[staff] = mod_pw

        overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
        if jailed_role: overwrites[jailed_role] = discord.PermissionOverwrite(view_channel=False)

        i = 1
        while discord.utils.get(guild.text_channels, name=name) is not None:
            i += 1; name = f"{base}-{i}"

        try:
            return await guild.create_text_channel(
                name=name, category=category, overwrites=overwrites,
                reason=f"WelcomeGate ID verification ticket for {member} ({member.id})"
            )
        except Exception:
            return None

    async def _post_ticket_intro(self, channel: discord.TextChannel, member: discord.Member):
        guild = member.guild; cfg = self.cfg(guild.id)
        security = resolve_role_any(guild, cfg.get("security_role_id"))
        staff = resolve_role_any(guild, cfg.get("staff_role_id"))
        pings = " ".join([x.mention for x in (security, staff) if x]) or ""
        emb = discord.Embed(
            title="ID Verification Required",
            description=(
                f"{member.mention}, to remain in the server you must complete **ID Verification**.\n"
                "• Upload a **clear photo of government ID** and a **note** with today's date and your Discord tag.\n"
                "• Cover non-essential info. **Screenshots/cross will not be accepted.**\n"
                "• A moderator will review and respond here."
            ),
            color=discord.Color.orange(), timestamp=utcnow(),
        )
        try:
            await channel.send(content=pings or None, embed=emb, allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False))
        except Exception:
            pass

    async def _maybe_post_jail_info(self, member: discord.Member):
        guild = member.guild; cfg = self.cfg(guild.id)
        ch = resolve_channel_any(guild, cfg.get("jail_info_channel_id"))
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(
                    f"{member.mention} has been **jailed** pending ID verification. A private ticket was created.",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
            except Exception:
                pass

    # ---- commands ----
    @commands.has_permissions(administrator=True)
    @commands.command(name="welcomepublish")
    async def welcomepublish_cmd(self, ctx: commands.Context):
        """Publish or update the permanent Verify panel."""
        guild = ctx.guild
        if not guild: return await ctx.reply("❌ Run in a guild.")
        cfg = self.cfg(guild.id); ch = self._welcome_channel(guild)
        if not ch: return await ctx.reply("🚫 No channel available to post the panel.")
        embed = self._format_panel_embed(guild); view = WelcomePanelView(self)
        msg = None
        if isinstance(cfg.get("panel_message_id"), int):
            try: msg = await ch.fetch_message(cfg["panel_message_id"])
            except Exception: msg = None
        if msg:
            await msg.edit(embed=embed, view=view); await ctx.reply(f"✅ Updated panel in {ch.mention}")
        else:
            msg = await ch.send(embed=embed, view=view)
            cfg["panel_channel_id"] = ch.id; cfg["panel_message_id"] = msg.id
            await save_config(self.bot.config); await ctx.reply(f"✅ Published panel in {ch.mention}")

    @commands.has_permissions(administrator=True)
    @commands.command(name="welcomepanel")
    async def welcomepanel_cmd(self, ctx: commands.Context):
        """Open modal-only config console."""
        view = WelcomeConfigView(self, ctx)
        msg = await ctx.send(embed=discord.Embed(title="Welcome Gate — Config", description="Loading…", color=discord.Color.blurple()), view=view)
        view.panel_message = msg
        await view.render()


# ===== extension entrypoint =====
async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeGate(bot))
