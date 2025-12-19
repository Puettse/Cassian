# Feral_Kitty_FiFi/features/welcome_gate.py
from __future__ import annotations

import asyncio
import random
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from typing import Any, Dict, Optional, Set

import discord
from discord.ext import commands, tasks

from ..config import save_config
from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any
from ..utils.perms import can_manage_role


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
    "channel_id": None,          # legacy "welcome channel" (used as fallback)
    "panel_channel_id": None,    # channel where permanent panel lives
    "panel_message_id": None,    # message id of permanent panel embed
    "dm_user": False,            # legacy; DM now happens on Verify click
    "log_channel_id": None,
    "message": {
        "title": "Welcome!",
        "description": "Welcome to the server, {mention}.\nClick **Verify** below to get started.",
        "image_url": "",
    },
    "autorole_id": None,         # role to give on join (e.g., Gated)
    "jailed_role_id": None,      # role to assign on appeal rejoin
    "security_role_id": None,    # role to ping on appeal rejoin
    "staff_role_id": None,       # role to ping on appeal rejoin
    "minimum_age": 18,           # years
    "appeals": {},               # {str(user_id): iso_timestamp}
    "challenge": {
        "enabled": True,
        "timeout_hours": 72,
        "max_attempts": 5,
        "remove_role_id": None,  # gated role to remove upon success
        "grant_role_id": None,   # replacement role to grant upon success (verified)
    },
}


class WelcomeGate(commands.Cog):
    """Welcome system: permanent Verify panel; DM+modal flow with DOB & passphrase; underage kick+appeal; jailed on rejoin."""

    VERIFY_BTN_ID = "welcome_gate:verify"
    APPEAL_BTN_ID = "welcome_gate:appeal"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active: Dict[int, Challenge] = {}
        self._used_codes: Set[str] = set()
        self._sweeper.start()

        # persistent views survive restarts
        self.bot.add_view(WelcomePanelView(self))      # Verify button
        self.bot.add_view(AppealView(self))            # Appeal button in DM

        if not getattr(self.bot, "intents", None) or not self.bot.intents.members:
            print("[WelcomeGate] ERROR: Intents.members disabled; on_member_join won’t fire.")

    def cog_unload(self):
        self._sweeper.cancel()

    # ---------- config ----------
    def cfg(self, guild_id: int) -> Dict[str, Any]:
        all_cfg = self.bot.config.setdefault("welcome_gate", {})
        for k, v in DEFAULT_CFG.items():
            all_cfg.setdefault(k, v if not isinstance(v, dict) else v.copy())
        # ensure nested defaults
        all_cfg.setdefault("challenge", {}).setdefault("enabled", True)
        return all_cfg

    # ---------- helpers ----------
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
        emb.set_footer(text="Press Verify to continue")
        return emb

    def _format_dm_code_embed(self, member: discord.Member, code: str, hours: int) -> discord.Embed:
        emb = discord.Embed(
            title="Verification Code",
            description=(
                f"Hi {member.mention},\n"
                f"Use the code below in the popup and complete your DOB.\n"
                f"**Code:** `{code}`\n"
                f"Expires in **{hours}h**."
            ),
            color=discord.Color.green(),
            timestamp=utcnow(),
        )
        return emb

    def _welcome_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        cfg = self.cfg(guild.id)
        for key in ("panel_channel_id", "channel_id"):
            ch = resolve_channel_any(guild, cfg.get(key))
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
                return ch
        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            return guild.system_channel
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if perms.read_messages and perms.send_messages:
                return ch
        return None

    # ---------- sweeper ----------
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

    # ---------- events ----------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        cfg = self.cfg(guild.id)
        if not cfg.get("enabled", True):
            return

        # autorole (gated)
        auto = resolve_role_any(guild, cfg.get("autorole_id"))
        if auto and can_manage_role(guild, auto):
            try:
                await member.add_roles(auto, reason="WelcomeGate autorole")
            except Exception:
                pass

        # appeal rejoin path → jail + ping staff/security
        appeals = cfg.setdefault("appeals", {})
        if str(member.id) in appeals:
            jailed = resolve_role_any(guild, cfg.get("jailed_role_id"))
            if jailed and can_manage_role(guild, jailed):
                try:
                    await member.add_roles(jailed, reason="WelcomeGate appeal rejoin → jailed")
                except Exception:
                    pass
            # ping roles in log channel
            sec = resolve_role_any(guild, cfg.get("security_role_id"))
            staff = resolve_role_any(guild, cfg.get("staff_role_id"))
            ping_text = " ".join([r.mention for r in (sec, staff) if r]) or ""
            await self._log(
                guild,
                f"{ping_text} 🚨 Appeal rejoin: {member.mention} is jailed pending ID verification. Screenshots/cross will not be accepted."
            )
            appeals.pop(str(member.id), None)
            await save_config(self.bot.config)

        # DO NOT send welcome message here; panel is permanent

    # ---------- verify flow ----------
    async def _start_verification_flow(self, interaction: discord.Interaction, member: discord.Member):
        guild = member.guild
        cfg = self.cfg(guild.id)
        chall_cfg = cfg.get("challenge") or {}

        code = None
        if chall_cfg.get("enabled", True):
            hours = clamp(int(chall_cfg.get("timeout_hours") or 72), 1, 72)
            code = self._gen_code()
            self._active[member.id] = Challenge(
                user_id=member.id,
                code=code,
                expires_at=utcnow() + timedelta(hours=hours),
            )
            # DM code
            try:
                await member.send(embed=self._format_dm_code_embed(member, code, hours))
            except Exception:
                await self._log(guild, f"✉️ DM failed for {member.mention} during verify.")
        # open modal
        await interaction.response.send_modal(VerifyModal(self, guild.id, member.id, require_code=bool(code)))

    async def _handle_verify_submission(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        user_id: int,
        dob_text: str,
        code_text: Optional[str],
    ):
        guild = interaction.guild or self.bot.get_guild(guild_id)
        if not guild:
            return await interaction.response.send_message("❌ Guild context missing.", ephemeral=True)

        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if not member:
            return await interaction.response.send_message("❌ Member not found.", ephemeral=True)

        cfg = self.cfg(guild.id)
        min_age = int(cfg.get("minimum_age") or 18)

        dob = _parse_yyyy_mm_dd(dob_text)
        if not dob:
            return await interaction.response.send_message("❌ DOB format must be YYYY-MM-DD.", ephemeral=True)
        age = _calc_age(dob)

        # Under 18 → sorry + kick after 10s + DM with appeal
        if age < min_age:
            await interaction.response.send_message(
                f"🚫 Sorry, you must be **{min_age}+**. You will be removed in 10 seconds.",
                ephemeral=True,
            )
            await self._log(guild, f"🚫 Underage ({age}) → scheduling kick for {member.mention}.")
            # schedule kick
            async def _kick_then_dm():
                await asyncio.sleep(10)
                try:
                    await member.kick(reason=f"WelcomeGate underage ({age})")
                except Exception as e:
                    await self._log(guild, f"⚠️ Kick failed for {member.mention}: {type(e).__name__}")
                    return
                # DM with appeal button (can DM after kick)
                try:
                    view = AppealView(self)
                    await member.send(
                        embed=discord.Embed(
                            title="Removed: Age Requirement",
                            description=(
                                f"You appear to be under **{min_age}** based on the DOB provided.\n\n"
                                "If this was an error, press **Request Rejoin (Appeal)** below to rejoin **jailed** while "
                                "Security/Staff review your ID. Screenshots/cross will not be accepted."
                            ),
                            color=discord.Color.red(),
                            timestamp=utcnow(),
                        ),
                        view=view,
                    )
                except Exception:
                    pass
            asyncio.create_task(_kick_then_dm())
            return

        # Age OK → require code if challenge enabled
        chall_cfg = cfg.get("challenge") or {}
        if chall_cfg.get("enabled", True):
            ch = self._active.get(member.id)
            if not ch or ch.expired():
                self._active.pop(member.id, None)
                return await interaction.followup.send("⏱️ Your verification session expired. Click Verify again.", ephemeral=True)
            max_attempts = clamp(int(chall_cfg.get("max_attempts") or 5), 1, 10)
            if ch.attempts >= max_attempts:
                self._active.pop(member.id, None)
                return await interaction.followup.send("❌ Attempts exceeded. Ask staff.", ephemeral=True)
            if not code_text or code_text.strip() != ch.code:
                ch.attempts += 1
                remain = max_attempts - ch.attempts
                return await interaction.response.send_message(
                    f"❌ Incorrect passcode. Attempts left: **{remain}**.", ephemeral=True
                )
            # success → swap roles
            gated = resolve_role_any(guild, chall_cfg.get("remove_role_id"))
            grant = resolve_role_any(guild, chall_cfg.get("grant_role_id"))
            if gated and can_manage_role(guild, gated) and gated in member.roles:
                try:
                    await member.remove_roles(gated, reason="WelcomeGate verified - remove gated")
                except Exception:
                    pass
            if grant and can_manage_role(guild, grant) and grant not in member.roles:
                try:
                    await member.add_roles(grant, reason="WelcomeGate verified - grant")
                except Exception:
                    pass
            self._used_codes.add(ch.code); self._active.pop(member.id, None)

        await interaction.response.send_message("✅ Verified. Welcome!", ephemeral=True)
        await self._log(guild, f"✅ {member.mention} verified (age {age}).")

    # ---------- commands ----------
    @commands.has_permissions(administrator=True)
    @commands.command(name="welcomepublish")
    async def welcomepublish_cmd(self, ctx: commands.Context):
        """Publish or update the permanent Verify panel."""
        guild = ctx.guild
        if not guild:
            return await ctx.reply("❌ Run in a guild.")

        cfg = self.cfg(guild.id)
        ch = self._welcome_channel(guild)
        if not ch:
            return await ctx.reply("🚫 No channel available to post the panel.")

        embed = self._format_panel_embed(guild)
        view = WelcomePanelView(self)

        # update existing if present
        msg_id = cfg.get("panel_message_id")
        msg = None
        if isinstance(msg_id, int):
            try:
                msg = await ch.fetch_message(msg_id)
            except Exception:
                msg = None

        if msg:
            await msg.edit(embed=embed, view=view)
            await ctx.reply(f"✅ Updated panel in {ch.mention}")
        else:
            msg = await ch.send(embed=embed, view=view)
            cfg["panel_channel_id"] = ch.id
            cfg["panel_message_id"] = msg.id
            await save_config(self.bot.config)
            await ctx.reply(f"✅ Published panel in {ch.mention}")

    @commands.has_permissions(administrator=True)
    @commands.command(name="welcomepanel")
    async def welcomepanel_cmd(self, ctx: commands.Context):
        """Open the config console (unchanged from your previous modal-based editor or keep your version)."""
        await ctx.send("ℹ️ Use `!welcomepublish` to post the permanent Verify panel. Use your config UI to change text/roles.")

    # existing commands (pass, etc.) can remain if you still want them, but the modal path supersedes it.


# ---------- UI: Persistent Verify Panel ----------
class WelcomePanelView(discord.ui.View):
    def __init__(self, cog: WelcomeGate):
        # persistent view: timeout=None and fixed custom_id
        super().__init__(timeout=None)
        self.cog = cog
        # attach the button dynamically to ensure custom_id is set on restart-safe view
        self.add_item(
            discord.ui.Button(
                label="Verify",
                style=discord.ButtonStyle.primary,
                custom_id=WelcomeGate.VERIFY_BTN_ID,
            )
        )

    @discord.ui.button(label="__hidden__", style=discord.ButtonStyle.secondary, disabled=True)
    async def _placeholder(self, *_):  # never used; avoids empty-view serialization issues
        pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):  # not used for persistent views
        pass

    async def on_error(self, error: Exception, item: discord.ui.Item, interaction: discord.Interaction):
        try:
            await interaction.response.send_message("⚠️ Something went wrong.", ephemeral=True)
        except Exception:
            pass

    async def interaction(self, interaction: discord.Interaction):
        pass  # unused; handled below via on_button_click


# map custom_id → handler (discord.py handles this when view is registered)
@discord.ui.button(label="Verify", style=discord.ButtonStyle.primary, custom_id=WelcomeGate.VERIFY_BTN_ID)
async def _handle_verify_button(interaction: discord.Interaction):
    cog = interaction.client.get_cog("WelcomeGate")
    if not isinstance(cog, WelcomeGate):
        try:
            await interaction.response.send_message("❌ Cog not ready.", ephemeral=True)
        except Exception:
            pass
        return
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if not member:
        return await interaction.response.send_message("❌ Member context required.", ephemeral=True)
    await cog._start_verification_flow(interaction, member)


# ---------- Modal: DOB + Passcode ----------
class VerifyModal(discord.ui.Modal):
    def __init__(self, cog: WelcomeGate, guild_id: int, user_id: int, require_code: bool):
        super().__init__(title="Verification", timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.require_code = require_code

        self.dob = discord.ui.TextInput(
            label="Date of Birth (YYYY-MM-DD)",
            placeholder="2004-07-15",
            required=True,
            max_length=10,
        )
        self.add_item(self.dob)

        self.code = discord.ui.TextInput(
            label="Passcode (sent to your DMs)",
            placeholder="Enter 6 digits (check your DM)",
            required=require_code,
            max_length=16,
        )
        if require_code:
            self.add_item(self.code)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_verify_submission(
            interaction,
            self.guild_id,
            self.user_id,
            self.dob.value,
            self.code.value if self.require_code else None,
        )


# ---------- Appeal: DM button to request rejoin ----------
class AppealView(discord.ui.View):
    def __init__(self, cog: WelcomeGate):
        super().__init__(timeout=3600)
        self.cog = cog
        self.add_item(
            discord.ui.Button(
                label="Request Rejoin (Appeal)",
                style=discord.ButtonStyle.primary,
                custom_id=WelcomeGate.APPEAL_BTN_ID,
            )
        )

    @discord.ui.button(label="__hidden__", style=discord.ButtonStyle.secondary, disabled=True)
    async def _placeholder(self, *_):  # serialization helper
        pass

    @discord.ui.button(label="Request Rejoin (Appeal)", style=discord.ButtonStyle.primary, custom_id=WelcomeGate.APPEAL_BTN_ID)
    async def _appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        if not isinstance(user, (discord.User, discord.Member)):
            return await interaction.response.send_message("❌ Invalid user.", ephemeral=True)

        # Find a guild where this cog is active and the user was kicked from (this DM path only makes sense for the same guild)
        # If your bot is single-guild, you can store guild_id in the DM prior. For simplicity, pick the first mutual guild.
        if not user.mutual_guilds:
            return await interaction.response.send_message("❌ No mutual servers found.", ephemeral=True)
        guild = user.mutual_guilds[0]
        cfg = self.cog.cfg(guild.id)

        # mark appeal flag (why: jail on rejoin)
        cfg.setdefault("appeals", {})[str(user.id)] = utcnow().isoformat()
        await save_config(self.cog.bot.config)

        # create 1-use invite
        ch = self.cog._welcome_channel(guild)
        invite_url = None
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).create_instant_invite:
            try:
                invite = await ch.create_invite(max_uses=1, max_age=3600, unique=True, reason="WelcomeGate appeal rejoin")
                invite_url = invite.url
            except Exception:
                invite_url = None

        if not invite_url:
            await interaction.response.send_message(
                "✅ Appeal recorded. Ask a moderator for an invite link to rejoin; you’ll be **jailed** on entry.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ Appeal recorded. Use this link to rejoin (1 use, 1 hour): {invite_url}\n"
            "On join, you’ll be **jailed** until Security/Staff review your ID.",
            ephemeral=True,
        )
# discord.py v2+ (preferred)
async def setup(bot: commands.Bot):
    """Extension entrypoint required by discord.py: adds the cog."""
    await bot.add_cog(WelcomeGate(bot))

# If you're on older discord.py that expects a sync entrypoint, use this instead:
# def setup(bot: commands.Bot):
#     bot.add_cog(WelcomeGate(bot))
