# Feral_Kitty_FiFi/features/welcome_gate.py
from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

import discord
from discord.ext import commands, tasks

from ..config import save_config
from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any
from ..utils.perms import can_manage_role


# ===== HARD-CODED CONSTANTS (per request) =====
TICKET_CATEGORY_ID = 1400849393652990083          # [Req. Verify] category
LOG_CHANNEL_ID = 1438922658636107847              # all gateway logs AND age-check embeds
MIN_AGE = 18                                       # hard age limit
PASSCODE_TIMEOUT_H = 48                            # 48 hours
PASSCODE_ATTEMPTS = 4                              # 4 tries
TICKET_PREFIX = "id-verify"                        # ticket name prefix


# ===== helpers =====
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


# ===== minimal config (only roles remain configurable) =====
DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,
    "autorole_id": None,      # GATED on join
    "remove_role_id": None,   # remove on verify (usually GATED)
    "grant_role_id": None,    # grant on verify (Member)
    "jailed_role_id": None,   # apply if underage
    "security_role_id": None, # can manage/see tickets, pinged
    "staff_role_id": None,    # can manage/see tickets, pinged
    # runtime state (do not edit by hand)
    "tickets": {},            # {str(user_id): channel_id}
}


@dataclass
class Challenge:
    user_id: int
    code: str
    expires_at: datetime
    attempts: int = 0

    def expired(self) -> bool:
        return utcnow() >= self.expires_at


# ===== Modals & Views =====
class AgeModal(discord.ui.Modal):
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

        # Log this age check (immutable log; we won't edit it later)
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

        # Age OK → issue passcode and prompt to enter (ephemeral only)
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
        view = _PasscodePromptView(self.cog, guild.id, member.id)
        await interaction.response.send_message(embed=emb, view=view, ephemeral=True)
        await self.cog._log(guild, f"🔐 Passcode issued to {member.mention} (age {age}).")


class _PasscodePromptView(discord.ui.View):
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


# ===== Cog =====
class WelcomeGate(commands.Cog):
    """Slash-only, no public panel. Flow:
    /agecheck → DOB modal → (underage → jail + ticket) or (age OK → passcode modal → verify).
    Logs: all actions and immutable age-check embeds in hard-coded LOG_CHANNEL_ID.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._challenges: Dict[int, Challenge] = {}  # user_id -> Challenge
        self._sweeper.start()
        if not getattr(self.bot, "intents", None) or not self.bot.intents.members:
            print("[WelcomeGate] ERROR: Intents.members disabled; member events limited.")

    def cog_unload(self):
        self._sweeper.cancel()

    # --- config ---
    def cfg(self, guild_id: int) -> Dict[str, Any]:
        all_cfg = self.bot.config.setdefault("welcome_gate", {})
        for k, v in DEFAULT_CFG.items():
            all_cfg.setdefault(k, v if not isinstance(v, dict) else v.copy())
        all_cfg.setdefault("tickets", {})
        return all_cfg

    # --- utils ---
    async def _log(self, guild: discord.Guild, text: str):
        ch = resolve_channel_any(guild, LOG_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def _log_age_check_embed(self, guild: discord.Guild, member: discord.Member, dob: date):
        ch = resolve_channel_any(guild, LOG_CHANNEL_ID)
        if not isinstance(ch, discord.TextChannel):
            return
        emb = discord.Embed(
            title="Age Check Submitted",
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        emb.add_field(name="User", value=f"{member} ({member.mention})", inline=False)
        emb.add_field(name="User ID", value=str(member.id), inline=True)
        emb.add_field(name="DOB Entered", value=dob.isoformat(), inline=True)
        emb.set_thumbnail(url=member.display_avatar.url if member.display_avatar else discord.Embed.Empty)
        # no edits performed later → “immutable log”
        await ch.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())

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

    # --- background cleanup ---
    @tasks.loop(minutes=5)
    async def _sweeper(self):
        expired = [uid for uid, ch in list(self._challenges.items()) if ch.expired()]
        for uid in expired:
            self._challenges.pop(uid, None)

    @_sweeper.before_loop
    async def _before_sweeper(self):
        await self.bot.wait_until_ready()

    # --- events ---
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = self.cfg(member.guild.id)
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
        tickets: Dict[str, int] = cfg.setdefault("tickets", {})
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
            await self._log(guild, f"🚫 {member} left during verification — banned (age verification evasion).")

    # --- slash command entrypoint ---
    @commands.hybrid_command(name="agecheck", description="Open the age check popup (DOB).")
    async def agecheck_cmd(self, ctx: commands.Context):
        if not isinstance(ctx.author, discord.Member):
            return await ctx.reply("❌ Run in a server.", ephemeral=True)  # type: ignore[arg-type]
        await ctx.interaction.response.send_modal(AgeModal(self, ctx.guild.id, ctx.author.id))  # type: ignore[union-attr]

    # --- passcode finalize ---
    async def _finalize_passcode(self, member: discord.Member, user_code: str) -> tuple[bool, str]:
        guild = member.guild
        ch = self._challenges.get(member.id)
        if not ch or ch.expired():
            self._challenges.pop(member.id, None)
            return False, "⏱️ Session expired. Run **/agecheck** again."

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

    # --- jail & ticket ---
    async def _jail_and_open_id_ticket(self, member: discord.Member):
        guild = member.guild
        cfg = self.cfg(guild.id)

        jailed = resolve_role_any(guild, cfg.get("jailed_role_id"))
        if not jailed:
            await self._log(guild, f"⚠️ jailed_role_id not set; cannot jail {member.mention}.")
            return

        # strip manageable roles
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

        # create ticket in hard-coded category
        channel = await self._create_ticket_channel(member)
        if channel:
            await self._post_ticket_intro(channel, member)
            # remember for leave handling
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
            # Add close button for staff
            await channel.send(view=TicketCloseView(self))
        except Exception:
            pass

    async def _archive_ticket_channel(self, channel: discord.TextChannel, reason: str = "Closed"):
        # Deny everyone including member; lock posting; rename with -closed
        try:
            overwrites = channel.overwrites
            for target, pw in list(overwrites.items()):
                pw.send_messages = False
                overwrites[target] = pw
            await channel.edit(name=f"{channel.name}-closed", overwrites=overwrites, reason=reason)
        except Exception:
            pass
        await self._log(channel.guild, f"📦 Archived ticket {channel.mention}: {reason}")

    # --- ticket close command (fallback) ---
    @commands.has_permissions(manage_channels=True)
    @commands.hybrid_command(name="closeticket", description="Close the current ticket.")
    async def closeticket_cmd(self, ctx: commands.Context):
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.reply("❌ Run inside a ticket channel.")
        await self._archive_ticket_channel(ctx.channel, reason=f"Closed by {ctx.author}")
        # drop from index if it was tracked
        cfg = self.cfg(ctx.guild.id)
        for uid, cid in list(cfg.get("tickets", {}).items()):
            if cid == ctx.channel.id:
                cfg["tickets"].pop(uid, None)
                await save_config(self.bot.config)
                break


class TicketCloseView(discord.ui.View):
    """Small view to close ticket; staff/security only (permission-gated)."""
    def __init__(self, cog: WelcomeGate):
        super().__init__(timeout=0)
        self.cog = cog
        btn = discord.ui.Button(label="Close Ticket", style=discord.ButtonStyle.danger)
        btn.callback = self._close
        self.add_item(btn)

    async def _close(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Not a text channel.", ephemeral=True)
        # basic gate: needs manage_channels OR has security/staff role
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
        # remove index entry if present
        for uid, cid in list(cfg.get("tickets", {}).items()):
            if cid == interaction.channel.id:
                cfg["tickets"].pop(uid, None)
                await save_config(self.cog.bot.config)
                break


# ===== extension entrypoint =====
async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeGate(bot))
