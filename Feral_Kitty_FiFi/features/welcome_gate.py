# Feral_Kitty_FiFi/features/welcome_gate.py
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

import discord
from discord.ext import commands, tasks


# ===== HARD-CODED CONSTANTS =====
TICKET_CATEGORY_ID = 1400849393652990083         # [Req. Verify] category
LOG_CHANNEL_ID     = 1438922658636107847         # gateway logs

ROLE_GATED_NAME    = "GATED"
ROLE_MEMBER_NAME   = "Member"
ROLE_JAILED_NAME   = "jailed"
ROLE_SECURITY_NAME = "Security"
ROLE_STAFF_NAME    = "Staff"

MIN_AGE             = 18
PASSCODE_TIMEOUT_H  = 48
PASSCODE_ATTEMPTS   = 4

VERIFY_BTN_ID       = "welcome_gate:age_check"   # persistent
TICKET_PREFIX       = "id-verify"

WELCOME_TITLE = "Welcome!"
WELCOME_DESC  = (
    "Please begin verification by pressing **Age Check** below.\n\n"
    "❗ **Warning:** The DOB you provide must match your ID during verification, "
    "or a ban may be enforced."
)
WELCOME_IMAGE_URL = "https://media.discordapp.net/attachments/1400677535980978246/1451675296356106412/dopmine.png?ex=69470979&is=6945b7f9&hm=9efc6616444af77651283979ff34a3552e513c5743e54d4b164b9e9db4cad230&=&format=webp&quality=lossless&width=3044&height=1712"

PASSCODE_TITLE = "Your Passcode"
PASSCODE_DESC  = (
    "Use the **Enter Passcode** button below to open a popup and submit this code.\n\n"
    "**Code:** `{code}`\n"
    f"Expires in **{PASSCODE_TIMEOUT_H}h**.\n\n"
    "⚠️ The DOB you provided must match your ID during verification, otherwise a ban may be enforced."
)
PASSCODE_IMAGE_URL = "https://media.discordapp.net/attachments/1400677535980978246/1451567820944052286/Where_Thick_Thighs_Meet_SSRIs_20251219_073130_0000.png?ex=6946a561&is=694553e1&hm=ff7072adbe8b0c0c64675bb6a54471b7a88920dd8b63d08e1744e6c6238b412e&=&format=webp&quality=lossless&width=2996&height=1712"


# ===== HELPERS =====
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _slug_username(member: discord.Member) -> str:
    base = member.name.lower()
    base = re.sub(r"[^a-z0-9\-]+", "-", base)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    return base or "user"

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

def _resolve_role_by_name(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    low = (name or "").lower()
    for r in guild.roles:
        if r.name.lower() == low:
            return r
    return None

def _can_manage_role(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return bool(me) and role < me.top_role and not role.is_default()


# ===== RUNTIME =====
@dataclass
class Challenge:
    user_id: int
    code: str
    expires_at: datetime
    attempts: int = 0

    def expired(self) -> bool:
        return utcnow() >= self.expires_at


# ===== VIEWS & MODALS =====
class WelcomePanelView(discord.ui.View):
    """Persistent 'Age Check' button; used on the public panel in the GATED channel."""
    def __init__(self, cog: "WelcomeGate"):
        super().__init__(timeout=None)
        self.cog = cog
        btn = discord.ui.Button(label="Age Check", style=discord.ButtonStyle.primary, custom_id=VERIFY_BTN_ID)
        btn.callback = self._on_age_check_clicked
        self.add_item(btn)

    async def _on_age_check_clicked(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)
        # Open DOB modal directly (panel lives in GATED channel; no private verify channel step)
        await interaction.response.send_modal(AgeModal(self.cog, interaction.guild.id, interaction.user.id))


class AgeModal(discord.ui.Modal):
    """DOB input modal."""
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

        # Immutable log
        await self.cog._log_age_check_embed(guild, member, dob)

        age = _calc_age(dob)
        if age < MIN_AGE:
            await interaction.response.send_message(
                f"🚫 You must be **{MIN_AGE}+**. You have been placed in **jail** and an **ID verification ticket** was opened.",
                ephemeral=True,
            )
            await self.cog._jail_and_open_ticket(member)
            await self.cog._log(guild, f"🚨 Underage ({age}) → jailed & ticket for {member.mention}.")
            return

        # Adult → passcode modal flow (no channel creation)
        code = self.cog._start_or_refresh_challenge(member)
        embed = self.cog._passcode_embed(guild, code)
        view = PasscodePromptView(self.cog, guild.id, member.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await self.cog._log(guild, f"🔐 Passcode issued to {member.mention}.")


class PasscodePromptView(discord.ui.View):
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int):
        super().__init__(timeout=300)
        self.cog = cog; self.guild_id = guild_id; self.user_id = user_id
        btn = discord.ui.Button(label="Enter Passcode", style=discord.ButtonStyle.success)
        btn.callback = self._open_modal
        self.add_item(btn)

    async def _open_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PasscodeModal(self.cog, self.guild_id, self.user_id))


class PasscodeModal(discord.ui.Modal):
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int):
        super().__init__(title="Enter Passcode", timeout=120)
        self.cog = cog; self.guild_id = guild_id; self.user_id = user_id
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


class TicketCloseView(discord.ui.View):
    def __init__(self, cog: "WelcomeGate"):
        super().__init__(timeout=0)
        self.cog = cog
        btn = discord.ui.Button(label="Close Ticket", style=discord.ButtonStyle.danger)
        btn.callback = self._close
        self.add_item(btn)

    async def _close(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Not a text channel.", ephemeral=True)
        guild = interaction.guild
        roles = getattr(interaction.user, "roles", [])
        sec = _resolve_role_by_name(guild, ROLE_SECURITY_NAME)
        staff = _resolve_role_by_name(guild, ROLE_STAFF_NAME)
        allowed = interaction.user.guild_permissions.manage_channels or any(r in roles for r in (sec, staff) if r)
        if not allowed:
            return await interaction.response.send_message("❌ You cannot close tickets.", ephemeral=True)
        await interaction.response.send_message("Archiving…", ephemeral=True)
        await self.cog._archive_ticket_channel(interaction.channel, reason=f"Closed by {interaction.user}")


# ===== COG =====
class WelcomeGate(commands.Cog):
    """GATED channel panel → DOB modal → passcode (adult) OR jailed+ticket (underage)."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._challenges: Dict[int, Challenge] = {}   # user_id -> Challenge
        self._tickets: Dict[int, int] = {}            # user_id -> channel_id
        self._sweeper.start()
        self.bot.add_view(WelcomePanelView(self))     # persistent button
        if not getattr(self.bot, "intents", None) or not self.bot.intents.members:
            print("[WelcomeGate] WARNING: Intents.members disabled; on_member_join won’t fire.")

    def cog_unload(self):
        self._sweeper.cancel()

    # ----- Embeds / Logs -----
    def _panel_embed(self, guild: discord.Guild) -> discord.Embed:
        emb = discord.Embed(title=WELCOME_TITLE, description=WELCOME_DESC, color=discord.Color.blurple(), timestamp=utcnow())
        if WELCOME_IMAGE_URL:
            emb.set_image(url=WELCOME_IMAGE_URL)
        return emb

    def _passcode_embed(self, guild: discord.Guild, code: str) -> discord.Embed:
        desc = PASSCODE_DESC.replace("{code}", code)
        emb = discord.Embed(title=PASSCODE_TITLE, description=desc, color=discord.Color.green(), timestamp=utcnow())
        if PASSCODE_IMAGE_URL:
            emb.set_image(url=PASSCODE_IMAGE_URL)
        return emb

    async def _log(self, guild: discord.Guild, text: str):
        ch = guild.get_channel(LOG_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def _log_age_check_embed(self, guild: discord.Guild, member: discord.Member, dob: date):
        ch = guild.get_channel(LOG_CHANNEL_ID)
        if not isinstance(ch, discord.TextChannel):
            return
        emb = discord.Embed(title="Age Check Submitted", color=discord.Color.blurple(), timestamp=utcnow())
        emb.add_field(name="User", value=f"{member} ({member.mention})", inline=False)
        emb.add_field(name="User ID", value=str(member.id), inline=True)
        emb.add_field(name="DOB Entered", value=dob.isoformat(), inline=True)
        emb.set_thumbnail(url=member.display_avatar.url if member.display_avatar else discord.Embed.Empty)
        await ch.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())

    # ----- Challenge -----
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

        # Success → remove GATED, grant Member
        gated = _resolve_role_by_name(guild, ROLE_GATED_NAME)
        member_role = _resolve_role_by_name(guild, ROLE_MEMBER_NAME)
        if gated and _can_manage_role(guild, gated) and gated in member.roles:
            try: await member.remove_roles(gated, reason="WelcomeGate verified — remove GATED")
            except Exception: pass
        if member_role and _can_manage_role(guild, member_role) and member_role not in member.roles:
            try: await member.add_roles(member_role, reason="WelcomeGate verified — grant Member")
            except Exception: pass

        self._challenges.pop(member.id, None)
        await self._log(guild, f"✅ {member.mention} verified (passcode).")
        return True, "✅ Verified. Welcome!"

    # ----- Ticket helpers (underage only) -----
    async def _jail_and_open_ticket(self, member: discord.Member):
        guild = member.guild
        # strip manageable roles; add JAILED
        jailed = _resolve_role_by_name(guild, ROLE_JAILED_NAME)
        if jailed:
            to_remove = [r for r in member.roles if not r.is_default() and _can_manage_role(guild, r)]
            if to_remove:
                try: await member.remove_roles(*to_remove, reason="WelcomeGate age check failed → jail")
                except Exception: pass
            if _can_manage_role(guild, jailed) and jailed not in member.roles:
                try: await member.add_roles(jailed, reason="WelcomeGate age check failed → jail")
                except Exception: pass
        else:
            await self._log(guild, f"⚠️ JAILED role '{ROLE_JAILED_NAME}' not found; cannot jail {member.mention}.")

        # create private id-verify-<user> ticket under category
        category = guild.get_channel(TICKET_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            return await self._log(guild, "⚠️ Ticket category missing or invalid.")

        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)

        sec = _resolve_role_by_name(guild, ROLE_SECURITY_NAME)
        staff = _resolve_role_by_name(guild, ROLE_STAFF_NAME)
        mod_pw = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True, manage_messages=True
        )
        if sec:   overwrites[sec]   = mod_pw
        if staff and staff != sec: overwrites[staff] = mod_pw

        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        )

        base = f"{TICKET_PREFIX}-{_slug_username(member)}"
        name = base; i = 1
        while discord.utils.get(guild.text_channels, name=name) is not None:
            i += 1; name = f"{base}-{i}"

        try:
            ch = await guild.create_text_channel(
                name=name, category=category, overwrites=overwrites,
                reason=f"Age verification required for {member} ({member.id})"
            )
        except Exception:
            return

        # post intro + close button, ping roles
        sec_r = _resolve_role_by_name(guild, ROLE_SECURITY_NAME)
        staff_r = _resolve_role_by_name(guild, ROLE_STAFF_NAME)
        pings = " ".join(x.mention for x in (sec_r, staff_r) if x) or ""
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
            await ch.send(content=pings or None, embed=emb, allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False))
            await ch.send(view=TicketCloseView(self))
        except Exception:
            pass

        self._tickets[member.id] = ch.id

    async def _archive_ticket_channel(self, channel: discord.TextChannel, reason: str = "Closed"):
        try:
            overwrites = channel.overwrites
            for target, pw in list(overwrites.items()):
                pw.send_messages = False
                overwrites[target] = pw
            await channel.edit(name=f"{channel.name}-closed", overwrites=overwrites, reason=reason)
        except Exception:
            pass
        await self._log(channel.guild, f"📦 Archived ticket {channel.mention}: {reason}")

    # ----- Events -----
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        gated = _resolve_role_by_name(member.guild, ROLE_GATED_NAME)
        if gated and _can_manage_role(member.guild, gated):
            try: await member.add_roles(gated, reason="WelcomeGate autorole (GATED)")
            except Exception: pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        ch_id = self._tickets.pop(member.id, None)
        if ch_id:
            ch = guild.get_channel(ch_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send("🚪 User left. Initiating ban for **age verification evasion**.", allowed_mentions=discord.AllowedMentions.none())
                except Exception: pass
                try: await guild.ban(discord.Object(member.id), reason="age verification evasion")
                except Exception: pass
                await self._archive_ticket_channel(ch, reason="Member left — auto-archive (age verification evasion)")
            await self._log(guild, f"🚫 {member} left during verification — banned (age verification evasion).")

    # ----- Background cleanup -----
    @tasks.loop(minutes=5)
    async def _sweeper(self):
        expired = [uid for uid, ch in list(self._challenges.items()) if ch.expired()]
        for uid in expired:
            self._challenges.pop(uid, None)

    @_sweeper.before_loop
    async def _before_sweeper(self):
        await self.bot.wait_until_ready()

    # ----- Admin command: publish/update panel (run with `$welcome`) -----
    @commands.has_permissions(administrator=True)
    @commands.command(name="welcome")  # call with $welcome
    async def publish_panel(self, ctx: commands.Context):
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.reply("❌ Run in a text channel.")
        embed = self._panel_embed(ctx.guild)
        view = WelcomePanelView(self)

        # Try to update an existing panel; else post new
        msg_to_edit: Optional[discord.Message] = None
        try:
            async for m in ctx.channel.history(limit=50):
                if m.author.id == ctx.bot.user.id and m.components:
                    if any(getattr(c, "custom_id", None) == VERIFY_BTN_ID for row in m.components for c in row.children):
                        msg_to_edit = m
                        break
        except Exception:
            msg_to_edit = None

        if msg_to_edit:
            await msg_to_edit.edit(embed=embed, view=view)
            await ctx.reply("✅ Updated Verify panel here.")
        else:
            await ctx.send(embed=embed, view=view)
            await ctx.reply("✅ Published Verify panel here.")


# ===== EXTENSION ENTRYPOINT =====
async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeGate(bot))
