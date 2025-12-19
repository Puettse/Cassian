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

    def expired(self) -> bool:
        return utcnow() >= self.expires_at


DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,

    # Permanent panel placement
    "panel_channel_id": None,
    "panel_message_id": None,

    # Logging (all actions go here; no public chat spam)
    "log_channel_id": None,

    # Text shown on the permanent panel
    "message": {
        "title": "Welcome!",
        "description": (
            "Welcome to the server, {mention}.\n\n"
            "Please **enter your Date of Birth** and press **Age Check**.\n"
            "❗ **Warning:** The DOB you provide must match your ID during verification, "
            "or a ban may be enforced."
        ),
        "image_url": "",
    },

    # Roles
    "autorole_id": None,          # [GATED] on join
    "remove_role_id": None,       # role to remove on successful verify (usually GATED)
    "grant_role_id": None,        # role to grant on successful verify (e.g., Member)
    "jailed_role_id": None,       # applied if DOB < minimum_age

    # Jailing / Ticketing
    "jail_info_channel_id": None, # message to announce jail (optional)
    "ticket_category_id": None,   # category to create ID-verify tickets
    "ticket_prefix": "id-verify", # ticket channel prefix
    "security_role_id": None,     # ping + access in ticket
    "staff_role_id": None,        # ping + access in ticket

    # Age requirement
    "minimum_age": 18,

    # Passcode challenge (after age OK)
    "challenge": {
        "enabled": True,
        "timeout_hours": 2,
        "max_attempts": 5,
    },
}

# Persistent button ids
VERIFY_BTN_ID = "welcome_gate:verify"          # on panel (Age Check)
PASSCODE_BTN_ID = "welcome_gate:enter_code"    # ephemeral, to open passcode modal


# ===== persistent verify panel =====
class WelcomePanelView(discord.ui.View):
    """Persistent panel with 'Age Check' (custom_id + timeout=None)."""
    def __init__(self, cog: "WelcomeGate"):
        super().__init__(timeout=None)
        self.cog = cog
        btn = discord.ui.Button(label="Age Check", style=discord.ButtonStyle.primary, custom_id=VERIFY_BTN_ID)
        btn.callback = self._on_age_check_clicked
        self.add_item(btn)

    async def _on_age_check_clicked(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)
        await self.cog._open_age_modal(interaction, interaction.user)


# Ephemeral “Enter Passcode” button (non-persistent)
class _PasscodePromptView(discord.ui.View):
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id

        btn = discord.ui.Button(label="Enter Passcode", style=discord.ButtonStyle.success, custom_id=None)
        btn.callback = self._open_modal
        self.add_item(btn)

    async def _open_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PasscodeModal(self.cog, self.guild_id, self.user_id))


class AgeModal(discord.ui.Modal):
    """DOB modal (first step)."""
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int, min_age: int):
        super().__init__(title="Age Check", timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.min_age = min_age

        self.dob = discord.ui.TextInput(
            label="Date of Birth (YYYY-MM-DD)",
            placeholder="2004-07-15",
            required=True,
            max_length=10,
        )
        self.add_item(self.dob)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild or self.cog.bot.get_guild(self.guild_id)
        if not guild:
            return await interaction.response.send_message("❌ Guild context missing.", ephemeral=True)

        member = guild.get_member(self.user_id) or await guild.fetch_member(self.user_id)
        if not member:
            return await interaction.response.send_message("❌ Member not found.", ephemeral=True)

        dob = _parse_yyyy_mm_dd(self.dob.value)
        if not dob:
            return await interaction.response.send_message("❌ DOB must be YYYY-MM-DD.", ephemeral=True)

        age = _calc_age(dob)
        if age < self.min_age:
            await interaction.response.send_message(
                f"🚫 You must be **{self.min_age}+**. You have been placed in **jail** and an **ID verification ticket** was opened.",
                ephemeral=True,
            )
            await self.cog._log(guild, f"🚨 Underage ({age}) → jailing & ticket for {member.mention}.")
            await self.cog._jail_and_open_id_ticket(member)
            return

        # Age OK → issue passcode + ephemeral prompt to enter it
        code = self.cog._start_or_refresh_challenge(member)
        emb = discord.Embed(
            title="Your Passcode",
            description=(
                "Use the **Enter Passcode** button below to open a popup and submit this code.\n\n"
                f"**Code:** `{code}`\n"
                f"Expires in **{self.cog._challenge_timeout_hours(guild)}h**."
            ),
            color=discord.Color.green(),
            timestamp=utcnow(),
        )
        view = _PasscodePromptView(self.cog, guild.id, member.id)
        await interaction.response.send_message(embed=emb, view=view, ephemeral=True)
        await self.cog._log(guild, f"🔐 Issued passcode to {member.mention} (age {age}).")


class PasscodeModal(discord.ui.Modal):
    """Second step: enter the passcode."""
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int):
        super().__init__(title="Enter Passcode", timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id

        self.code = discord.ui.TextInput(
            label="6-digit Passcode",
            placeholder="000000",
            required=True,
            max_length=16,
        )
        self.add_item(self.code)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild or self.cog.bot.get_guild(self.guild_id)
        if not guild:
            return await interaction.response.send_message("❌ Guild context missing.", ephemeral=True)

        try:
            member = guild.get_member(self.user_id) or await guild.fetch_member(self.user_id)
        except Exception:
            member = None
        if not member:
            return await interaction.response.send_message("❌ Member not found.", ephemeral=True)

        ok, msg = await self.cog._finalize_passcode(member, self.code.value)
        await interaction.response.send_message(msg, ephemeral=True)
        await self.cog._log(guild, f"{'✅' if ok else '❌'} Passcode result for {member.mention}: {msg}")


# ===== modal-only config console =====
class WelcomeConfigView(discord.ui.View):
    """Admin config console using Modals only (no chat entry)."""
    panel_message: Optional[discord.Message] = None

    def __init__(self, cog: "WelcomeGate", ctx: commands.Context):
        super().__init__(timeout=600)
        self.cog = cog
        self.ctx = ctx

    async def render(self):
        if not self.panel_message:
            return
        g = self.ctx.guild
        cfg = self.cog.cfg(g.id)

        def _role(rid):
            r = resolve_role_any(g, rid)
            return r.mention if r else str(rid)

        def _chan(cid):
            c = resolve_channel_any(g, cid)
            return c.mention if isinstance(c, discord.TextChannel) else str(cid)

        def _cat(cid):
            c = resolve_channel_any(g, cid)
            return c.name if isinstance(c, discord.CategoryChannel) else str(cid)

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

        emb.add_field(name="Gated (Autorole on Join)", value=_role(cfg.get("autorole_id")), inline=True)
        emb.add_field(name="Remove on Verify", value=_role(cfg.get("remove_role_id")), inline=True)
        emb.add_field(name="Grant on Verify", value=_role(cfg.get("grant_role_id")), inline=True)

        emb.add_field(name="Jailed Role", value=_role(cfg.get("jailed_role_id")), inline=True)
        emb.add_field(name="Security Role", value=_role(cfg.get("security_role_id")), inline=True)
        emb.add_field(name="Staff Role", value=_role(cfg.get("staff_role_id")), inline=True)

        emb.add_field(name="Ticket Category", value=_cat(cfg.get("ticket_category_id")), inline=True)
        emb.add_field(name="Jail Info Channel", value=_chan(cfg.get("jail_info_channel_id")), inline=True)
        emb.add_field(name="Panel Channel", value=_chan(cfg.get("panel_channel_id")), inline=True)
        emb.add_field(name="Log Channel", value=_chan(cfg.get("log_channel_id")), inline=True)

        chcfg = cfg.get("challenge") or {}
        emb.add_field(name="Passcode Enabled", value=str(bool(chcfg.get("enabled", True))), inline=True)
        emb.add_field(name="Passcode Timeout (h)", value=str(int(chcfg.get("timeout_hours") or 0)), inline=True)
        emb.add_field(name="Max Attempts", value=str(int(chcfg.get("max_attempts") or 0)), inline=True)

        emb.add_field(name="Message Title", value=(message.get("title") or "Welcome!"), inline=True)
        preview = (message.get("description") or "")[:180] or "_empty_"
        emb.add_field(name="Message Preview", value=preview, inline=False)
        if message.get("image_url"):
            emb.set_image(url=message["image_url"])

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

    # ----- role/channel/category setters -----
    class _SetRole( _BaseModal ):
        def __init__(self, view, label, key):
            super().__init__(view, f"Set {label}")
            self.key = key
            self.t = discord.ui.TextInput(label="Role mention / ID / [Exact Name] (or 'clear')", required=True, max_length=100)
            self.add_item(self.t)

        async def on_submit(self, interaction: discord.Interaction):
            g = self.view.ctx.guild
            cfg = self.view.cog.cfg(g.id)
            s = self.t.value.strip()
            if s.lower() == "clear":
                cfg[self.key] = None
                return await self._ok(interaction, "✅ Cleared.")
            r = resolve_role_any(g, s)
            if not r:
                return await self._fail(interaction, "❌ Role not found.")
            cfg[self.key] = r.id
            await self._ok(interaction, f"✅ Set to {r.mention}.")

    class _SetTextChan(_BaseModal):
        def __init__(self, view, label, key):
            super().__init__(view, f"Set {label}")
            self.key = key
            self.t = discord.ui.TextInput(label="Channel mention / ID / exact name (or 'clear')", required=True, max_length=100)
            self.add_item(self.t)

        async def on_submit(self, interaction: discord.Interaction):
            g = self.view.ctx.guild
            cfg = self.view.cog.cfg(g.id)
            s = self.t.value.strip()
            if s.lower() == "clear":
                cfg[self.key] = None
                return await self._ok(interaction, "✅ Cleared.")
            ch = resolve_channel_any(g, s)
            if not isinstance(ch, discord.TextChannel):
                return await self._fail(interaction, "❌ Not a text channel.")
            cfg[self.key] = ch.id
            await self._ok(interaction, f"✅ Set to {ch.mention}.")

    class _SetCategory(_BaseModal):
        def __init__(self, view, label, key):
            super().__init__(view, f"Set {label}")
            self.key = key
            self.t = discord.ui.TextInput(label="Category mention / ID / exact name (or 'clear')", required=True, max_length=100)
            self.add_item(self.t)

        async def on_submit(self, interaction: discord.Interaction):
            g = self.view.ctx.guild
            cfg = self.view.cog.cfg(g.id)
            s = self.t.value.strip()
            if s.lower() == "clear":
                cfg[self.key] = None
                return await self._ok(interaction, "✅ Cleared.")
            ch = resolve_channel_any(g, s)
            if not isinstance(ch, discord.CategoryChannel):
                return await self._fail(interaction, "❌ Not a category.")
            cfg[self.key] = ch.id
            await self._ok(interaction, f"✅ Set to {ch.name}.")

    class _SetMinAge(_BaseModal):
        def __init__(self, view):
            super().__init__(view, "Set Minimum Age")
            self.t = discord.ui.TextInput(label="Minimum Age (13–99)", required=True, max_length=3, default="18")
            self.add_item(self.t)

        async def on_submit(self, interaction: discord.Interaction):
            try:
                val = max(13, min(99, int(self.t.value.strip())))
            except ValueError:
                return await self._fail(interaction, "❌ Numbers only.")
            cfg = self.view.cog.cfg(self.view.ctx.guild.id)
            cfg["minimum_age"] = val
            await self._ok(interaction, f"✅ Minimum age set to {val}.")

    class _SetTicketPrefix(_BaseModal):
        def __init__(self, view):
            super().__init__(view, "Set Ticket Prefix")
            self.t = discord.ui.TextInput(label="Ticket Prefix", required=True, max_length=24, default="id-verify")
            self.add_item(self.t)

        async def on_submit(self, interaction: discord.Interaction):
            val = self.t.value.strip().lower() or "id-verify"
            cfg = self.view.cog.cfg(self.view.ctx.guild.id)
            cfg["ticket_prefix"] = val
            await self._ok(interaction, f"✅ Ticket prefix set to `{val}`.")

    class _SetMessage(_BaseModal):
        def __init__(self, view):
            super().__init__(view, "Set Welcome Message")
            self.title_in = discord.ui.TextInput(label="Title", required=False, max_length=256)
            self.desc_in = discord.ui.TextInput(
                label="Description (supports {mention})",
                style=discord.TextStyle.paragraph,
                required=False,
                max_length=2000,
            )
            self.img_in = discord.ui.TextInput(label="Image URL (optional)", required=False, max_length=512)
            self.add_item(self.title_in); self.add_item(self.desc_in); self.add_item(self.img_in)

        async def on_submit(self, interaction: discord.Interaction):
            cfg = self.view.cog.cfg(self.view.ctx.guild.id)
            m = cfg.setdefault("message", {})
            if self.title_in.value is not None:
                m["title"] = self.title_in.value.strip()
            if self.desc_in.value is not None:
                m["description"] = self.desc_in.value.strip()
            if self.img_in.value is not None:
                m["image_url"] = self.img_in.value.strip()
            await self._ok(interaction, "✅ Message updated.")

    class _SetChallenge(_BaseModal):
        def __init__(self, view):
            super().__init__(view, "Passcode Options")
            self.enabled = discord.ui.TextInput(label="Enabled (true/false)", required=True, default="true", max_length=5)
            self.timeout = discord.ui.TextInput(label="Timeout hours (1–72)", required=True, default="2", max_length=3)
            self.attempts = discord.ui.TextInput(label="Max attempts (1–10)", required=True, default="5", max_length=2)
            self.add_item(self.enabled); self.add_item(self.timeout); self.add_item(self.attempts)

        async def on_submit(self, interaction: discord.Interaction):
            cfg = self.view.cog.cfg(self.view.ctx.guild.id)
            ch = cfg.setdefault("challenge", {})
            ch["enabled"] = (self.enabled.value.strip().lower() in {"1", "true", "yes", "on"})
            try:
                ch["timeout_hours"] = clamp(int(self.timeout.value.strip()), 1, 72)
                ch["max_attempts"] = clamp(int(self.attempts.value.strip()), 1, 10)
            except ValueError:
                return await self._fail(interaction, "❌ timeout/attempts must be numbers.")
            await self._ok(interaction, "✅ Passcode options updated.")

    # ----- Buttons -> Modals / actions -----
    @discord.ui.button(label="Toggle Enabled", style=discord.ButtonStyle.primary, row=0)
    async def _toggle_enabled(self, interaction: discord.Interaction, _: discord.ui.Button):
        cfg = self.cog.cfg(self.ctx.guild.id)
        cfg["enabled"] = not bool(cfg.get("enabled", True))
        await save_config(self.cog.bot.config)
        await interaction.response.send_message(f"Enabled → {cfg['enabled']}", ephemeral=True)
        await self.render()

    @discord.ui.button(label="Minimum Age", style=discord.ButtonStyle.secondary, row=0)
    async def _min_age(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetMinAge(self))

    @discord.ui.button(label="Ticket Prefix", style=discord.ButtonStyle.secondary, row=0)
    async def _ticket_prefix(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetTicketPrefix(self))

    @discord.ui.button(label="Gated (Autorole on Join)", style=discord.ButtonStyle.secondary, row=1)
    async def _set_auto(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetRole(self, "Gated Autorole", "autorole_id"))

    @discord.ui.button(label="Remove on Verify", style=discord.ButtonStyle.secondary, row=1)
    async def _set_remove(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetRole(self, "Role to Remove on Verify", "remove_role_id"))

    @discord.ui.button(label="Grant on Verify", style=discord.ButtonStyle.secondary, row=1)
    async def _set_grant(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetRole(self, "Role to Grant on Verify", "grant_role_id"))

    @discord.ui.button(label="Jailed Role", style=discord.ButtonStyle.secondary, row=2)
    async def _set_jailed(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetRole(self, "Jailed Role", "jailed_role_id"))

    @discord.ui.button(label="Security Role", style=discord.ButtonStyle.secondary, row=2)
    async def _set_security(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetRole(self, "Security Role", "security_role_id"))

    @discord.ui.button(label="Staff Role", style=discord.ButtonStyle.secondary, row=2)
    async def _set_staff(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetRole(self, "Staff Role", "staff_role_id"))

    @discord.ui.button(label="Ticket Category", style=discord.ButtonStyle.secondary, row=3)
    async def _set_ticket_cat(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetCategory(self, "Ticket Category", "ticket_category_id"))

    @discord.ui.button(label="Jail Info Channel", style=discord.ButtonStyle.secondary, row=3)
    async def _set_jail_info(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetTextChan(self, "Jail Info Channel", "jail_info_channel_id"))

    @discord.ui.button(label="Panel Channel", style=discord.ButtonStyle.secondary, row=3)
    async def _set_panel_chan(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetTextChan(self, "Panel Channel (where Verify lives)", "panel_channel_id"))

    @discord.ui.button(label="Log Channel", style=discord.ButtonStyle.secondary, row=4)
    async def _set_log_chan(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetTextChan(self, "Log Channel", "log_channel_id"))

    @discord.ui.button(label="Passcode Options", style=discord.ButtonStyle.secondary, row=4)
    async def _set_challenge(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetChallenge(self))

    @discord.ui.button(label="Welcome Text/Image", style=discord.ButtonStyle.success, row=4)
    async def _set_msg(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self._SetMessage(self))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, row=4)
    async def _close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("👋 Closed.", ephemeral=True)
        if self.panel_message:
            await self.panel_message.edit(view=None)
        self.stop()


# ===== cog =====
class WelcomeGate(commands.Cog):
    """Permanent panel → Age modal → Passcode → verify, or jail+ticket if underage. Modal-only admin console."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active: Dict[int, Challenge] = {}
        self._used_codes: Set[str] = set()
        self._sweeper.start()
        self.bot.add_view(WelcomePanelView(self))  # persistent panel

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
    def _challenge_timeout_hours(self, guild: discord.Guild) -> int:
        ch = (self.cfg(guild.id).get("challenge") or {})
        return clamp(int(ch.get("timeout_hours") or 2), 1, 72)

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

    def _panel_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = self.cfg(guild.id)
        msg = cfg.get("message") or {}
        emb = discord.Embed(
            title=(msg.get("title") or "Welcome!"),
            description=(msg.get("description") or "").replace("{mention}", "{mention}"),
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        if msg.get("image_url"):
            emb.set_image(url=msg["image_url"])
        emb.set_footer(text="Enter DOB and press Age Check")
        return emb

    def _welcome_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        cfg = self.cfg(guild.id)
        ch = resolve_channel_any(guild, cfg.get("panel_channel_id"))
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch
        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            return guild.system_channel
        for c in guild.text_channels:
            p = c.permissions_for(guild.me)
            if p.read_messages and p.send_messages:
                return c
        return None

    # ---- background ----
    @tasks.loop(minutes=5)
    async def _sweeper(self):
        expired = [uid for uid, ch in list(self._active.items()) if ch.expired()]
        for uid in expired:
            self._active.pop(uid, None)
        if len(self._used_codes) > 20000:
            for code in list(self._used_codes)[:5000]:
                self._used_codes.discard(code)

    @_sweeper.before_loop
    async def _before_sweeper(self):
        await self.bot.wait_until_ready()

    # ---- events ----
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        cfg = self.cfg(guild.id)
        if not cfg.get("enabled", True):
            return
        # Apply [GATED] autorole on join
        auto = resolve_role_any(guild, cfg.get("autorole_id"))
        if auto and can_manage_role(guild, auto):
            try:
                await member.add_roles(auto, reason="WelcomeGate autorole (GATED)")
            except Exception:
                pass

    # ---- public flow (buttons & modals) ----
    async def _open_age_modal(self, interaction: discord.Interaction, member: discord.Member):
        guild = member.guild
        min_age = int(self.cfg(guild.id).get("minimum_age") or 18)
        await interaction.response.send_modal(AgeModal(self, guild.id, member.id, min_age))

    def _start_or_refresh_challenge(self, member: discord.Member) -> str:
        guild = member.guild
        code = self._gen_code()
        hours = self._challenge_timeout_hours(guild)
        self._active[member.id] = Challenge(
            user_id=member.id,
            code=code,
            expires_at=utcnow() + timedelta(hours=hours),
        )
        return code

    async def _finalize_passcode(self, member: discord.Member, user_code: str) -> tuple[bool, str]:
        guild = member.guild
        cfg = self.cfg(guild.id)
        chall_cfg = cfg.get("challenge") or {}
        if not chall_cfg.get("enabled", True):
            return False, "Challenge disabled."

        ch = self._active.get(member.id)
        if not ch or ch.expired():
            self._active.pop(member.id, None)
            return False, "⏱️ Session expired. Press **Age Check** again."

        max_attempts = clamp(int(chall_cfg.get("max_attempts") or 5), 1, 10)
        if ch.attempts >= max_attempts:
            self._active.pop(member.id, None)
            return False, "❌ Attempts exceeded. Contact staff."

        if (user_code or "").strip() != ch.code:
            ch.attempts += 1
            remain = max_attempts - ch.attempts
            return False, f"❌ Incorrect passcode. Attempts left: **{remain}**."

        # Success → swap roles
        removed = resolve_role_any(guild, cfg.get("remove_role_id"))
        granted = resolve_role_any(guild, cfg.get("grant_role_id"))

        if removed and can_manage_role(guild, removed) and removed in member.roles:
            try:
                await member.remove_roles(removed, reason="WelcomeGate verified — remove gated")
            except Exception:
                pass
        if granted and can_manage_role(guild, granted) and granted not in member.roles:
            try:
                await member.add_roles(granted, reason="WelcomeGate verified — grant")
            except Exception:
                pass

        self._used_codes.add(ch.code)
        self._active.pop(member.id, None)
        return True, "✅ Verified. Welcome!"

    # ---- jailing & ticket helpers ----
    async def _jail_and_open_id_ticket(self, member: discord.Member):
        guild = member.guild
        cfg = self.cfg(guild.id)
        jailed_role = resolve_role_any(guild, cfg.get("jailed_role_id"))
        if not jailed_role:
            return await self._log(guild, f"⚠️ jailed_role_id not set; cannot jail {member.mention}.")

        # Strip manageable roles (keep @everyone)
        to_remove = [r for r in member.roles if not r.is_default() and can_manage_role(guild, r)]
        if to_remove:
            try:
                await member.remove_roles(*to_remove, reason="WelcomeGate DOB failed → jail")
            except Exception:
                pass

        if can_manage_role(guild, jailed_role) and jailed_role not in member.roles:
            try:
                await member.add_roles(jailed_role, reason="WelcomeGate DOB failed → jail")
            except Exception:
                pass

        ch = await self._create_id_ticket_channel(member)
        if ch:
            await self._post_ticket_intro(ch, member)
        await self._maybe_post_jail_info(member)

    async def _create_id_ticket_channel(self, member: discord.Member) -> Optional[discord.TextChannel]:
        guild = member.guild
        cfg = self.cfg(guild.id)

        prefix = (cfg.get("ticket_prefix") or "id-verify").strip() or "id-verify"
        base = f"{prefix}-{_slug_username(member)}"
        name = base

        category = None
        cat = resolve_channel_any(guild, cfg.get("ticket_category_id"))
        if isinstance(cat, discord.CategoryChannel):
            category = cat

        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)

        security = resolve_role_any(guild, cfg.get("security_role_id"))
        staff = resolve_role_any(guild, cfg.get("staff_role_id"))
        jailed_role = resolve_role_any(guild, cfg.get("jailed_role_id"))

        mod_pw = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True, manage_messages=True
        )
        if security:
            overwrites[security] = mod_pw
        if staff and staff != security:
            overwrites[staff] = mod_pw

        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        )
        if jailed_role:
            overwrites[jailed_role] = discord.PermissionOverwrite(view_channel=False)

        i = 1
        while discord.utils.get(guild.text_channels, name=name) is not None:
            i += 1
            name = f"{base}-{i}"

        try:
            return await guild.create_text_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                reason=f"WelcomeGate ID verification ticket for {member} ({member.id})",
            )
        except Exception:
            return None

    async def _post_ticket_intro(self, channel: discord.TextChannel, member: discord.Member):
        guild = member.guild
        cfg = self.cfg(guild.id)
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
            color=discord.Color.orange(),
            timestamp=utcnow(),
        )
        try:
            await channel.send(
                content=pings or None,
                embed=emb,
                allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
            )
        except Exception:
            pass

    async def _maybe_post_jail_info(self, member: discord.Member):
        guild = member.guild
        cfg = self.cfg(guild.id)
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
        """Publish or update the permanent Verify panel (no duplicate send)."""
        guild = ctx.guild
        if not guild:
            return await ctx.reply("❌ Run in a guild.")

        cfg = self.cfg(guild.id)
        ch = self._welcome_channel(guild)
        if not ch:
            return await ctx.reply("🚫 No channel available to post the panel.")

        embed = self._panel_embed(guild)
        view = WelcomePanelView(self)

        msg = None
        msg_id = cfg.get("panel_message_id")
        if isinstance(msg_id, int):
            try:
                msg = await ch.fetch_message(msg_id)
            except Exception:
                msg = None

        if msg:
            await msg.edit(embed=embed, view=view)
            await ctx.reply(f"✅ Updated panel in {ch.mention}")
        else:
            posted = await ch.send(embed=embed, view=view)
            cfg["panel_channel_id"] = ch.id
            cfg["panel_message_id"] = posted.id
            await save_config(self.bot.config)
            await ctx.reply(f"✅ Published panel in {ch.mention}")

    @commands.has_permissions(administrator=True)
    @commands.command(name="welcomepanel")
    async def welcomepanel_cmd(self, ctx: commands.Context):
        """Open the modal-only config console (single message; edits in-place)."""
        view = WelcomeConfigView(self, ctx)
        # Send once, then render edits this message — prevents doubles.
        msg = await ctx.send(
            embed=discord.Embed(
                title="Welcome Gate — Config",
                description="Loading…",
                color=discord.Color.blurple(),
            ),
            view=view,
        )
        view.panel_message = msg
        await view.render()


# ===== extension entrypoint =====
async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeGate(bot))
