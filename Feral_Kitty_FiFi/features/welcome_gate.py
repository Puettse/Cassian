# Feral_Kitty_FiFi/features/welcome_gate.py
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

import discord
from discord.ext import commands, tasks

from ..config import save_config
from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any
from ..utils.perms import can_manage_role  # role hierarchy safety


# ===== HARD-CODED CONSTANTS (per requirements) =====
TICKET_CATEGORY_ID = 1400849393652990083          # [Req. Verify] category
LOG_CHANNEL_ID = 1438922658636107847              # all gateway logs + age-check embeds
MIN_AGE = 18
PASSCODE_TIMEOUT_H = 48
PASSCODE_ATTEMPTS = 4
TICKET_PREFIX = "id-verify"

# Persistent button custom IDs
VERIFY_BTN_ID = "welcome_gate:age_check"   # persistent
# (ephemeral passcode button uses a view-only button without global custom_id)


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


# ===== runtime data =====
@dataclass
class Challenge:
    user_id: int
    code: str
    expires_at: datetime
    attempts: int = 0

    def expired(self) -> bool:
        return utcnow() >= self.expires_at


# ===== minimal cfg (roles, panel placement, message, tickets) =====
DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,
    # roles
    "autorole_id": None,      # GATED on join
    "remove_role_id": None,   # remove on passcode success (usually GATED)
    "grant_role_id": None,    # grant on passcode success (e.g., Member)
    "jailed_role_id": None,   # applied if underage
    "security_role_id": None, # see/manage tickets, pinged
    "staff_role_id": None,    # see/manage tickets, pinged
    # tickets index {str(user_id): channel_id}
    "tickets": {},
    # panel placement + content
    "panel_channel_id": None,
    "panel_message_id": None,
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
}


# ===== Views & Modals (user flow) =====
class WelcomePanelView(discord.ui.View):
    """Persistent Verify panel (button opens DOB modal)."""
    def __init__(self, cog: "WelcomeGate"):
        super().__init__(timeout=None)
        self.cog = cog
        btn = discord.ui.Button(label="Age Check", style=discord.ButtonStyle.primary, custom_id=VERIFY_BTN_ID)
        btn.callback = self._on_age_check_clicked
        self.add_item(btn)

    async def _on_age_check_clicked(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)
        await interaction.response.send_modal(AgeModal(self.cog, interaction.guild.id, interaction.user.id))


class AgeModal(discord.ui.Modal):
    """First step: DOB input"""
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int):
        super().__init__(title="Age Check", timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.dob = discord.ui.TextInput(
            label="Date of Birth (YYYY-MM-DD)",
            placeholder="2007-01-23",
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

        # Immutable age-check log (we never edit this message)
        await self.cog._log_age_check_embed(guild, member, dob)

        age = _calc_age(dob)
        if age < MIN_AGE:
            await interaction.response.send_message(
                f"🚫 You must be **{MIN_AGE}+**. You have been placed in **jail** and an **ID verification ticket** was opened.",
                ephemeral=True,
            )
            await self.cog._jail_and_open_id_ticket(member)
            await self.cog._log(guild, f"🚨 Underage ({age}) → jailed & ticket for {member.mention}.")
            return

        # Age OK → issue challenge & prompt for passcode (ephemeral only)
        code = self.cog._start_or_refresh_challenge(member)
        emb = discord.Embed(
            title="Your Passcode",
            description=(
                "Use the **Enter Passcode** button below to open a popup and submit this code.\n\n"
                f"**Code:** `{code}`\n"
                f"Expires in **{PASSCODE_TIMEOUT_H}h**.\n\n"
                "⚠️ The DOB you provided must match your ID during verification, otherwise a ban may be enforced."
            ),
            color=discord.Color.green(),
            timestamp=utcnow(),
        )
        view = PasscodePromptView(self.cog, guild.id, member.id)
        await interaction.response.send_message(embed=emb, view=view, ephemeral=True)
        await self.cog._log(guild, f"🔐 Passcode issued to {member.mention} (age {age}).")


class PasscodePromptView(discord.ui.View):
    """Ephemeral 'Enter Passcode' opener."""
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        btn = discord.ui.Button(label="Enter Passcode", style=discord.ButtonStyle.success)
        btn.callback = self._open_modal
        self.add_item(btn)

    async def _open_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PasscodeModal(self.cog, self.guild_id, self.user_id))


class PasscodeModal(discord.ui.Modal):
    """Second step: passcode entry."""
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int):
        super().__init__(title="Enter Passcode", timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.code = discord.ui.TextInput(label="6-digit Passcode", placeholder="000000", required=True, max_length=16)
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


# ===== Admin Config — EPHEMERAL PICKERS (no chat typing) =====
class _RolePickView(discord.ui.View):
    """Ephemeral role selector that writes to config immediately."""
    def __init__(self, cog: "WelcomeGate", guild: discord.Guild, key: str, label: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild = guild
        self.key = key
        self.label = label

        class _RS(discord.ui.RoleSelect):
            def __init__(self, outer: "_RolePickView"):
                super().__init__(min_values=1, max_values=1)
                self.outer = outer

            async def callback(self, itx: discord.Interaction):
                role = self.values[0]
                cfg = self.outer.cog.cfg(self.outer.guild.id)
                cfg[self.outer.key] = role.id
                await save_config(self.outer.cog.bot.config)
                await itx.response.edit_message(content=f"✅ {self.outer.label} → {role.mention}", view=None)

        self.add_item(_RS(self))


class _TextChannelPickView(discord.ui.View):
    """Ephemeral text-channel selector (for panel channel)."""
    def __init__(self, cog: "WelcomeGate", guild: discord.Guild, key: str, label: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild = guild
        self.key = key
        self.label = label

        class _CS(discord.ui.ChannelSelect):
            def __init__(self, outer: "_TextChannelPickView"):
                super().__init__(channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
                self.outer = outer

            async def callback(self, itx: discord.Interaction):
                ch = self.values[0]
                cfg = self.outer.cog.cfg(self.outer.guild.id)
                cfg[self.outer.key] = ch.id
                await save_config(self.outer.cog.bot.config)
                await itx.response.edit_message(content=f"✅ {self.outer.label} → {ch.mention}", view=None)

        self.add_item(_CS(self))


class WelcomeConfigView(discord.ui.View):
    """Admin config console using **only** ephemeral pickers (no chat input)."""
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

        def _role_name(rid):
            r = resolve_role_any(g, rid)
            return r.mention if r else "None"

        def _chan_mention(cid):
            c = resolve_channel_any(g, cid)
            return c.mention if isinstance(c, discord.TextChannel) else "None"

        m = cfg.get("message") or {}
        emb = discord.Embed(
            title="Welcome Gate — Config",
            description="Click buttons to open **ephemeral pickers**. No chat typing. Changes save instantly.",
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        # Hard-coded (read-only)
        emb.add_field(name="Ticket Category (hard-coded)", value=f"<#{TICKET_CATEGORY_ID}>", inline=True)
        emb.add_field(name="Log Channel (hard-coded)", value=f"<#{LOG_CHANNEL_ID}>", inline=True)
        emb.add_field(name="Min Age / Timeout / Attempts", value=f"{MIN_AGE} / {PASSCODE_TIMEOUT_H}h / {PASSCODE_ATTEMPTS}", inline=True)

        # Roles (editable)
        emb.add_field(name="Autorole on Join (GATED)", value=_role_name(cfg.get("autorole_id")), inline=True)
        emb.add_field(name="Remove on Verify", value=_role_name(cfg.get("remove_role_id")), inline=True)
        emb.add_field(name="Grant on Verify", value=_role_name(cfg.get("grant_role_id")), inline=True)
        emb.add_field(name="Jailed Role", value=_role_name(cfg.get("jailed_role_id")), inline=True)
        emb.add_field(name="Security Role", value=_role_name(cfg.get("security_role_id")), inline=True)
        emb.add_field(name="Staff Role", value=_role_name(cfg.get("staff_role_id")), inline=True)

        # Panel placement + preview
        emb.add_field(name="Panel Channel", value=_chan_mention(cfg.get("panel_channel_id")), inline=True)
        emb.add_field(name="Message Title", value=(m.get("title") or "Welcome!"), inline=True)
        preview = (m.get("description") or "")[:200] or "_empty_"
        emb.add_field(name="Message Preview", value=preview, inline=False)
        if m.get("image_url"):
            emb.set_image(url=m["image_url"])

        await self.panel_message.edit(embed=emb, view=self)

    # Buttons → ephemeral pickers
    @discord.ui.button(label="Autorole (GATED)", style=discord.ButtonStyle.secondary, row=0)
    async def btn_auto(self, itx: discord.Interaction, _: discord.ui.Button):
        await itx.response.send_message(
            "Pick **Autorole (GATED)**",
            view=_RolePickView(self.cog, self.ctx.guild, "autorole_id", "Autorole (GATED)"),
            ephemeral=True,
        )

    @discord.ui.button(label="Remove on Verify", style=discord.ButtonStyle.secondary, row=0)
    async def btn_remove(self, itx: discord.Interaction, _: discord.ui.Button):
        await itx.response.send_message(
            "Pick **Role to Remove on Verify**",
            view=_RolePickView(self.cog, self.ctx.guild, "remove_role_id", "Remove on Verify"),
            ephemeral=True,
        )

    @discord.ui.button(label="Grant on Verify", style=discord.ButtonStyle.secondary, row=1)
    async def btn_grant(self, itx: discord.Interaction, _: discord.ui.Button):
        await itx.response.send_message(
            "Pick **Role to Grant on Verify**",
            view=_RolePickView(self.cog, self.ctx.guild, "grant_role_id", "Grant on Verify"),
            ephemeral=True,
        )

    @discord.ui.button(label="Jailed Role", style=discord.ButtonStyle.secondary, row=1)
    async def btn_jailed(self, itx: discord.Interaction, _: discord.ui.Button):
        await itx.response.send_message(
            "Pick **Jailed Role**",
            view=_RolePickView(self.cog, self.ctx.guild, "jailed_role_id", "Jailed Role"),
            ephemeral=True,
        )

    @discord.ui.button(label="Security Role", style=discord.ButtonStyle.secondary, row=2)
    async def btn_security(self, itx: discord.Interaction, _: discord.ui.Button):
        await itx.response.send_message(
            "Pick **Security Role**",
            view=_RolePickView(self.cog, self.ctx.guild, "security_role_id", "Security Role"),
            ephemeral=True,
        )

    @discord.ui.button(label="Staff Role", style=discord.ButtonStyle.secondary, row=2)
    async def btn_staff(self, itx: discord.Interaction, _: discord.ui.Button):
        await itx.response.send_message(
            "Pick **Staff Role**",
            view=_RolePickView(self.cog, self.ctx.guild, "staff_role_id", "Staff Role"),
            ephemeral=True,
        )

    @discord.ui.button(label="Panel Channel", style=discord.ButtonStyle.secondary, row=3)
    async def btn_panel_channel(self, itx: discord.Interaction, _: discord.ui.Button):
        await itx.response.send_message(
            "Pick **Panel Channel** (where Verify panel lives)",
            view=_TextChannelPickView(self.cog, self.ctx.guild, "panel_channel_id", "Panel Channel"),
            ephemeral=True,
        )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, row=3)
    async def btn_close(self, itx: discord.Interaction, _: discord.ui.Button):
        await itx.response.send_message("👋 Closed.", ephemeral=True)
        if self.panel_message:
            await self.panel_message.edit(view=None)
        self.stop()


# ===== Cog =====
class WelcomeGate(commands.Cog):
    """Prefix admin cmds + persistent Verify panel for user modals. Fully logged; hard-coded ticket & log destinations."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._challenges: Dict[int, Challenge] = {}
        self._sweeper.start()
        # register persistent view at startup
        self.bot.add_view(WelcomePanelView(self))
        if not getattr(self.bot, "intents", None) or not self.bot.intents.members:
            print("[WelcomeGate] WARNING: Intents.members disabled; on_member_join won’t fire.")

    def cog_unload(self):
        self._sweeper.cancel()

    # ---- config access ----
    def cfg(self, guild_id: int) -> Dict[str, Any]:
        all_cfg = self.bot.config.setdefault("welcome_gate", {})
        for k, v in DEFAULT_CFG.items():
            all_cfg.setdefault(k, v if not isinstance(v, dict) else v.copy())
        all_cfg.setdefault("tickets", {})
        return all_cfg

    # ---- logging ----
    async def _log(self, guild: discord.Guild, text: str):
        ch = resolve_channel_any(guild, LOG_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def _log_age_check_embed(self, guild: discord.Guild, member: discord.Member, dob: date):
        ch = resolve_channel_any(guild, LOG_CHANNEL_ID)
        if not isinstance(ch, discord.TextChannel):
            return
        emb = discord.Embed(title="Age Check Submitted", color=discord.Color.blurple(), timestamp=utcnow())
        emb.add_field(name="User", value=f"{member} ({member.mention})", inline=False)
        emb.add_field(name="User ID", value=str(member.id), inline=True)
        emb.add_field(name="DOB Entered", value=dob.isoformat(), inline=True)
        emb.set_thumbnail(url=member.display_avatar.url if member.display_avatar else discord.Embed.Empty)
        await ch.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())

    # ---- challenge helpers ----
    def _gen_code(self) -> str:
        return f"{random.randint(0, 999_999):06d}"

    def _start_or_refresh_challenge(self, member: discord.Member) -> str:
        code = self._gen_code()
        self._challenges[member.id] = Challenge(
            user_id=member.id,
            code=code,
            expires_at=utcnow() + timedelta(hours=PASSCODE_TIMEOUT_H),
            attempts=0,
        )
        return code

    async def _finalize_passcode(self, member: discord.Member, user_code: str) -> tuple[bool, str]:
        guild = member.guild
        ch = self._challenges.get(member.id)
        if not ch or ch.expired():
            self._challenges.pop(member.id, None)
            return False, "⏱️ Session expired. Press **Age Check** again."

        if ch.attempts >= PASSCODE_ATTEMPTS:
            self._challenges.pop(member.id, None)
            return False, "❌ Attempts exceeded. Contact staff."

        if (user_code or "").strip() != ch.code:
            ch.attempts += 1
            remain = PASSCODE_ATTEMPTS - ch.attempts
            return False, f"❌ Incorrect. Attempts left: **{remain}**."

        # success → role swap
        cfg = self.cfg(guild.id)
        to_remove = resolve_role_any(guild, cfg.get("remove_role_id"))
        to_grant = resolve_role_any(guild, cfg.get("grant_role_id"))

        if to_remove and can_manage_role(guild, to_remove) and to_remove in member.roles:
            try:
                await member.remove_roles(to_remove, reason="WelcomeGate verified — remove gated")
            except Exception:
                pass
        if to_grant and can_manage_role(guild, to_grant) and to_grant not in member.roles:
            try:
                await member.add_roles(to_grant, reason="WelcomeGate verified — grant")
            except Exception:
                pass

        self._challenges.pop(member.id, None)
        await self._log(guild, f"✅ {member.mention} verified (passcode).")
        return True, "✅ Verified. Welcome!"

    # ---- background cleanup ----
    @tasks.loop(minutes=5)
    async def _sweeper(self):
        expired = [uid for uid, ch in list(self._challenges.items()) if ch.expired()]
        for uid in expired:
            self._challenges.pop(uid, None)

    @_sweeper.before_loop
    async def _before_sweeper(self):
        await self.bot.wait_until_ready()

    # ---- events ----
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = self.cfg(member.guild.id)
        if not cfg.get("enabled", True):
            return
        auto = resolve_role_any(member.guild, cfg.get("autorole_id"))
        if auto and can_manage_role(member.guild, auto):
            try:
                await member.add_roles(auto, reason="WelcomeGate autorole (GATED)")
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        cfg = self.cfg(guild.id)
        tickets = cfg.setdefault("tickets", {})
        ch_id = tickets.get(str(member.id))
        if ch_id:
            ch = guild.get_channel(ch_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send(
                        f"🚪 {member.mention} left the server. Initiating ban for **age verification evasion**.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception:
                    pass
                try:
                    await guild.ban(discord.Object(member.id), reason="age verification evasion")
                except Exception:
                    pass
                await self._archive_ticket_channel(ch, reason="Member left — auto-archive (age verification evasion)")
            tickets.pop(str(member.id), None)
            await save_config(self.bot.config)
            await self._log(guild, f"🚫 {member} left during verification — banned (age verification evasion).")

    # ---- admin prefix commands ----
    @commands.has_permissions(administrator=True)
    @commands.command(name="welcomepublish")
    async def welcomepublish_cmd(self, ctx: commands.Context):
        """Publish or update the permanent Verify panel."""
        guild = ctx.guild
        if not guild:
            return await ctx.reply("❌ Run in a guild.")
        cfg = self.cfg(guild.id)
        ch = resolve_channel_any(guild, cfg.get("panel_channel_id"))
        if not isinstance(ch, discord.TextChannel):
            # safe fallback: system channel or first writable text channel
            ch = guild.system_channel if isinstance(guild.system_channel, discord.TextChannel) else None
            if not ch:
                for c in guild.text_channels:
                    p = c.permissions_for(guild.me)
                    if p.read_messages and p.send_messages:
                        ch = c
                        break
        if not isinstance(ch, discord.TextChannel):
            return await ctx.reply("🚫 No channel to post the panel. Set 'Panel Channel' in !welcomepanel.")

        embed = self._panel_embed(guild)
        view = WelcomePanelView(self)

        msg = None
        if isinstance(cfg.get("panel_message_id"), int):
            try:
                msg = await ch.fetch_message(cfg["panel_message_id"])
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
        """Open the modal-only config console (ephemeral pickers)."""
        view = WelcomeConfigView(self, ctx)
        msg = await ctx.send(
            embed=discord.Embed(title="Welcome Gate — Config", description="Loading…", color=discord.Color.blurple()),
            view=view,
        )
        view.panel_message = msg
        await view.render()

    # ---- panel embed ----
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

    # ---- jail & ticket ----
    async def _jail_and_open_id_ticket(self, member: discord.Member):
        guild = member.guild
        cfg = self.cfg(guild.id)

        jailed = resolve_role_any(guild, cfg.get("jailed_role_id"))
        if not jailed:
            await self._log(guild, f"⚠️ jailed_role_id not set; cannot jail {member.mention}.")
            return

        # Strip manageable roles (keep @everyone)
        to_remove = [r for r in member.roles if not r.is_default() and can_manage_role(guild, r)]
        if to_remove:
            try:
                await member.remove_roles(*to_remove, reason="WelcomeGate age check failed → jail")
            except Exception:
                pass

        if jailed not in member.roles and can_manage_role(guild, jailed):
            try:
                await member.add_roles(jailed, reason="WelcomeGate age check failed → jail")
            except Exception:
                pass

        channel = await self._create_ticket_channel(member)
        if channel:
            await self._post_ticket_intro(channel, member)
            cfg.setdefault("tickets", {})[str(member.id)] = channel.id
            await save_config(self.bot.config)

    async def _create_ticket_channel(self, member: discord.Member) -> Optional[discord.TextChannel]:
        guild = member.guild
        category = resolve_channel_any(guild, TICKET_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            await self._log(guild, "⚠️ Ticket category missing or invalid.")
            return None

        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)

        cfg = self.cfg(guild.id)
        sec = resolve_role_any(guild, cfg.get("security_role_id"))
        staff = resolve_role_any(guild, cfg.get("staff_role_id"))
        mod_pw = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True, manage_messages=True
        )
        if sec:
            overwrites[sec] = mod_pw
        if staff and staff != sec:
            overwrites[staff] = mod_pw

        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        )

        base = f"{TICKET_PREFIX}-{_slug_username(member)}"
        name = base
        i = 1
        while discord.utils.get(guild.text_channels, name=name) is not None:
            i += 1
            name = f"{base}-{i}"

        try:
            ch = await guild.create_text_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                reason=f"WelcomeGate ID verification ticket for {member} ({member.id})",
            )
            return ch
        except Exception:
            return None

    async def _post_ticket_intro(self, channel: discord.TextChannel, member: discord.Member):
        guild = member.guild
        cfg = self.cfg(guild.id)
        sec = resolve_role_any(guild, cfg.get("security_role_id"))
        staff = resolve_role_any(guild, cfg.get("staff_role_id"))
        pings = " ".join([x.mention for x in (sec, staff) if x]) or ""
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
            await channel.send(view=TicketCloseView(self))
        except Exception:
            pass

    async def _archive_ticket_channel(self, channel: discord.TextChannel, reason: str = "Closed"):
        # lock down and rename with -closed
        try:
            overwrites = channel.overwrites
            for target, pw in list(overwrites.items()):
                pw.send_messages = False
                overwrites[target] = pw
            await channel.edit(name=f"{channel.name}-closed", overwrites=overwrites, reason=reason)
        except Exception:
            pass
        await self._log(channel.guild, f"📦 Archived ticket {channel.mention}: {reason}")


class TicketCloseView(discord.ui.View):
    """Close ticket (staff/security or Manage Channels)."""
    def __init__(self, cog: WelcomeGate):
        super().__init__(timeout=0)
        self.cog = cog
        btn = discord.ui.Button(label="Close Ticket", style=discord.ButtonStyle.danger)
        btn.callback = self._close
        self.add_item(btn)

    async def _close(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Not a text channel.", ephemeral=True)
        guild = interaction.guild
        cfg = self.cog.cfg(guild.id)
        allowed = interaction.user.guild_permissions.manage_channels
        if not allowed:
            sec = resolve_role_any(guild, cfg.get("security_role_id"))
            staff = resolve_role_any(guild, cfg.get("staff_role_id"))
            roles = getattr(interaction.user, "roles", [])
            allowed = any(r and r in roles for r in (sec, staff))
        if not allowed:
            return await interaction.response.send_message("❌ You cannot close tickets.", ephemeral=True)
        await interaction.response.send_message("Archiving…", ephemeral=True)
        await self.cog._archive_ticket_channel(interaction.channel, reason=f"Closed by {interaction.user}")
        # cleanup index
        cfg = self.cog.cfg(guild.id)
        for uid, cid in list(cfg.get("tickets", {}).items()):
            if cid == interaction.channel.id:
                cfg["tickets"].pop(uid, None)
                await save_config(self.cog.bot.config)
                break


# ===== extension entrypoint =====
async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeGate(bot))
