# Feral_Kitty_FiFi/features/welcome_gate.py
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple, List

import discord
from discord.ext import commands, tasks

from ..config import save_config  # persists bot.config
from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _parse_yyyy_mm_dd(s: str) -> Optional[date]:
    try:
        y, m, d = [int(p) for p in s.strip().split("-")]
        return date(y, m, d)
    except Exception:
        return None

def _calc_age(dob: date, today: Optional[date] = None) -> int:
    t = today or utcnow().date()
    years = t.year - dob.year
    if (t.month, t.day) < (dob.month, dob.day):
        years -= 1
    return years

def _slug_username(member: discord.Member) -> str:
    base = member.name.lower()
    base = re.sub(r"[^a-z0-9\-]+", "-", base)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    return base or "user"

def _resolve_role_flex(guild: discord.Guild, token: Any) -> Optional[discord.Role]:
    """Accepts id, mention, exact name, or [Exact Name] – case-insensitive for names."""
    # Prefer your shared resolver if present
    r = resolve_role_any(guild, token)
    if r:
        return r
    # case-insensitive fallback by name
    s = str(token or "").strip()
    if s.startswith("@"):
        s = s[1:]
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    low = s.casefold()
    return next((rr for rr in guild.roles if rr.name.casefold() == low), None)

def _can_manage_role(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return bool(me) and (not role.is_default()) and (not role.managed) and (role < me.top_role)

# ---------- config access ----------
def age_cfg(bot: commands.Bot) -> Dict[str, Any]:
    """
    bot.config["age_gate"] layout:
    {
      "enabled": true,
      "min_age": 18,
      "passcode": { "timeout_hours": 48, "max_attempts": 4 },
      "panel": {
        "image_url": "...",
        "title": "Welcome!",
        "description": "Press Age Check ..."
      },
      "roles": {
        "gated": "GATED",       // id or name or mention
        "member": "Member",
        "jailed": "jailed",
        "staff_ping": ["Staff","SECURITY"]  // list id/name/mentions
      },
      "channels": {
        "log_channel_id": 143123...,        // id or name/mention supported
        "ticket_category_id": 1400849...    // id or name; must be a CategoryChannel
      }
    }
    """
    root = bot.config.setdefault("age_gate", {})
    # sensible defaults
    root.setdefault("enabled", True)
    root.setdefault("min_age", 18)
    pc = root.setdefault("passcode", {})
    pc.setdefault("timeout_hours", 48)
    pc.setdefault("max_attempts", 4)
    pnl = root.setdefault("panel", {})
    pnl.setdefault("title", "Welcome!")
    pnl.setdefault("description", "Please begin verification by pressing **Age Check** below.")
    pnl.setdefault("image_url", "")
    roles = root.setdefault("roles", {})
    roles.setdefault("gated", "GATED")
    roles.setdefault("member", "Member")
    roles.setdefault("jailed", "jailed")
    roles.setdefault("staff_ping", ["Staff"])
    chs = root.setdefault("channels", {})
    chs.setdefault("log_channel_id", None)
    chs.setdefault("ticket_category_id", None)
    return root

def _resolve_staff_mentions(guild: discord.Guild, cfg: Dict[str, Any]) -> Tuple[str, List[int]]:
    ids: List[int] = []
    for token in (cfg.get("roles", {}).get("staff_ping") or []):
        r = _resolve_role_flex(guild, token)
        if r:
            ids.append(r.id)
    mentions = " ".join(guild.get_role(rid).mention for rid in ids if guild.get_role(rid))
    return mentions, ids

def _resolve_log_channel(guild: discord.Guild, cfg: Dict[str, Any]) -> Optional[discord.TextChannel]:
    tok = cfg.get("channels", {}).get("log_channel_id")
    ch = resolve_channel_any(guild, tok)
    return ch if isinstance(ch, discord.TextChannel) else None

def _resolve_ticket_category(guild: discord.Guild, cfg: Dict[str, Any]) -> Optional[discord.CategoryChannel]:
    tok = cfg.get("channels", {}).get("ticket_category_id")
    ch = resolve_channel_any(guild, tok)
    return ch if isinstance(ch, discord.CategoryChannel) else None

# ---------- runtime state ----------
@dataclass
class Challenge:
    user_id: int
    code: str
    expires_at: datetime
    attempts: int = 0

    def expired(self) -> bool:
        return utcnow() >= self.expires_at

# ---------- UI ----------
VERIFY_BTN_ID = "agegate:verify"

class WelcomePanelView(discord.ui.View):
    def __init__(self, cog: "WelcomeGate"):
        super().__init__(timeout=None)
        self.cog = cog
        btn = discord.ui.Button(label="Age Check", style=discord.ButtonStyle.primary, custom_id=VERIFY_BTN_ID)
        btn.callback = self._on_click
        self.add_item(btn)

    async def _on_click(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)

        cfg = age_cfg(self.cog.bot)
        if not cfg.get("enabled", True):
            return await interaction.response.send_message("Age gate is currently disabled.", ephemeral=True)

        # If already jailed, block retries
        jailed = _resolve_role_flex(interaction.guild, cfg["roles"].get("jailed"))
        if jailed and jailed in interaction.user.roles:
            return await interaction.response.send_message("🚫 You are in review. Please wait for staff.", ephemeral=True)

        await interaction.response.send_modal(AgeModal(self.cog, interaction.guild.id, interaction.user.id))

class AgeModal(discord.ui.Modal):
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int):
        super().__init__(title="Age Check", timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.dob = discord.ui.TextInput(
            label="Date of Birth (YYYY-MM-DD)",
            placeholder="2000-01-31",
            required=True,
            max_length=10,
        )
        self.add_item(self.dob)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild or self.cog.bot.get_guild(self.guild_id)
        if not guild:
            return await interaction.response.send_message("❌ Guild missing.", ephemeral=True)

        member = guild.get_member(self.user_id) or await guild.fetch_member(self.user_id)
        if not member:
            return await interaction.response.send_message("❌ Member not found.", ephemeral=True)

        dob = _parse_yyyy_mm_dd(self.dob.value)
        if not dob:
            return await interaction.response.send_message("❌ DOB must be YYYY-MM-DD.", ephemeral=True)

        await self.cog._log_age_check(guild, member, dob)

        min_age = int(age_cfg(self.cog.bot).get("min_age", 18))
        age = _calc_age(dob)
        if age < min_age:
            # Underage → jail + ticket + ping staff
            await self.cog._underage_action(member, age)
            return

        # Adult → passcode flow
        code = self.cog._start_or_refresh_challenge(member)
        await self.cog._send_passcode(interaction, guild, code)

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
        self.code = discord.ui.TextInput(label="6-digit Passcode", placeholder="123456", required=True, max_length=16)
        self.add_item(self.code)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild or self.cog.bot.get_guild(self.guild_id)
        if not guild:
            return await interaction.response.send_message("❌ Guild missing.", ephemeral=True)
        member = guild.get_member(self.user_id) or await guild.fetch_member(self.user_id)
        if not member:
            return await interaction.response.send_message("❌ Member not found.", ephemeral=True)

        ok, msg = await self.cog._finalize_passcode(member, self.code.value)
        await interaction.response.send_message(msg, ephemeral=True)
        ch = _resolve_log_channel(guild, age_cfg(self.cog.bot))
        if ch:
            try: await ch.send(f"{'✅' if ok else '❌'} Passcode result for {member.mention}: {msg}",
                               allowed_mentions=discord.AllowedMentions.none())
            except Exception: pass

# ---------- Cog ----------
class WelcomeGate(commands.Cog):
    """Panel → DOB → passcode (adult) OR jailed + ticket (underage)."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._challenges: Dict[int, Challenge] = {}   # user_id -> Challenge
        self._sweeper.start()
        self.bot.add_view(WelcomePanelView(self))     # persistent button

    def cog_unload(self):
        self._sweeper.cancel()

    # Embeds / messages
    def _panel_embed(self, guild: discord.Guild) -> discord.Embed:
        pnl = age_cfg(self.bot).get("panel", {})
        emb = discord.Embed(
            title=pnl.get("title") or "Welcome!",
            description=pnl.get("description") or "Press Age Check.",
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        if pnl.get("image_url"):
            emb.set_image(url=pnl["image_url"])
        return emb

    async def _log_age_check(self, guild: discord.Guild, member: discord.Member, dob: date):
        ch = _resolve_log_channel(guild, age_cfg(self.bot))
        if not ch: return
        emb = discord.Embed(title="Age Check Submitted", color=discord.Color.blurple(), timestamp=utcnow())
        emb.add_field(name="User", value=f"{member} ({member.mention})", inline=False)
        emb.add_field(name="User ID", value=str(member.id), inline=True)
        emb.add_field(name="DOB Entered", value=dob.isoformat(), inline=True)
        if member.display_avatar:
            emb.set_thumbnail(url=member.display_avatar.url)
        try:
            await ch.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    async def _underage_action(self, member: discord.Member, age_val: int):
        """Remove GATED, add JAILED, open private ticket, ping staff; block further tries."""
        guild = member.guild
        cfg = age_cfg(self.bot)
        roles_cfg = cfg.get("roles", {})

        gated = _resolve_role_flex(guild, roles_cfg.get("gated"))
        jailed = _resolve_role_flex(guild, roles_cfg.get("jailed"))

        # strip manageable roles; ensure jailed applied
        if gated and _can_manage_role(guild, gated) and gated in member.roles:
            try: await member.remove_roles(gated, reason="AgeGate: underage → remove GATED")
            except Exception: pass
        if jailed and _can_manage_role(guild, jailed) and jailed not in member.roles:
            try: await member.add_roles(jailed, reason="AgeGate: underage → add JAILED")
            except Exception: pass

        # create ticket channel under configured category
        cat = _resolve_ticket_category(guild, cfg)
        if not cat:
            # still tell the user + ping staff in log
            ch = _resolve_log_channel(guild, cfg)
            if ch:
                await ch.send(f"⚠️ Ticket category not configured; could not create channel for {member.mention}.",
                              allowed_mentions=discord.AllowedMentions.none())
            try:
                await member.send("You have been placed in review. A moderator will contact you shortly.")
            except Exception:
                pass
            return

        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)

        staff_ping_text, staff_ids = _resolve_staff_mentions(guild, cfg)
        staff_po = discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=True, attach_files=True, manage_messages=True
        )
        for rid in staff_ids:
            r = guild.get_role(rid)
            if r:
                overwrites[r] = staff_po

        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=True, attach_files=True
        )

        # unique name
        base = f"id-verify-{_slug_username(member)}"
        name = base; i = 1
        while discord.utils.get(guild.text_channels, name=name):
            i += 1; name = f"{base}-{i}"

        try:
            ch_ticket = await guild.create_text_channel(
                name=name, category=cat, overwrites=overwrites,
                reason=f"AgeGate underage review for {member} ({member.id})"
            )
        except Exception:
            ch_ticket = None

        # notify + ping
        if ch_ticket:
            intro = discord.Embed(
                title="ID Verification Required",
                description=(f"{member.mention}, you must complete ID verification to remain in the server.\n"
                             "• Upload a clear photo of your government ID and a handwritten note with **today’s date** and your Discord tag.\n"
                             "• Cover non-essential info. Screenshots/cross-server proof will not be accepted.\n"
                             "• A moderator will respond here."),
                color=discord.Color.orange(), timestamp=utcnow(),
            )
            try:
                await ch_ticket.send(content=(staff_ping_text or None), embed=intro,
                                     allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False))
            except Exception:
                pass

        # confirm to user (ephemeral was already sent by modal handler)
        logch = _resolve_log_channel(guild, cfg)
        if logch:
            try:
                await logch.send(f"🚨 Underage ({age_val}) → {member.mention} placed in **jailed** and ticket opened: {ch_ticket.mention if ch_ticket else 'N/A'}",
                                 allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass

    def _start_or_refresh_challenge(self, member: discord.Member) -> str:
        pcfg = age_cfg(self.bot).get("passcode", {})
        timeout_h = int(pcfg.get("timeout_hours") or 48)
        code = f"{random.randint(0, 999_999):06d}"
        self._challenges[member.id] = Challenge(
            user_id=member.id,
            code=code,
            expires_at=utcnow() + timedelta(hours=timeout_h),
            attempts=0,
        )
        return code

    async def _send_passcode(self, interaction: discord.Interaction, guild: discord.Guild, code: str):
        pnl = age_cfg(self.bot).get("panel", {})
        pcfg = age_cfg(self.bot).get("passcode", {})
        desc = (f"Use the **Enter Passcode** button to submit your code.\n\n"
                f"**Code:** `{code}`\n"
                f"Expires in **{int(pcfg.get('timeout_hours') or 48)}h**.")
        emb = discord.Embed(title="Your Passcode", description=desc, color=discord.Color.green(), timestamp=utcnow())
        if pnl.get("image_url"):
            emb.set_image(url=pnl["image_url"])
        view = PasscodePromptView(self, guild.id, interaction.user.id)
        await interaction.response.send_message(embed=emb, view=view, ephemeral=True)

    async def _finalize_passcode(self, member: discord.Member, user_code: str) -> Tuple[bool, str]:
        cfg = age_cfg(self.bot)
        pcfg = cfg.get("passcode", {})
        max_attempts = int(pcfg.get("max_attempts") or 4)

        ch = self._challenges.get(member.id)
        if not ch or ch.expired():
            self._challenges.pop(member.id, None)
            return False, "⏱️ Session expired. Press **Age Check** again."

        if ch.attempts >= max_attempts:
            self._challenges.pop(member.id, None)
            return False, "❌ Attempts exceeded. Contact staff."

        if (user_code or "").strip() != ch.code:
            ch.attempts += 1
            remain = max(0, max_attempts - ch.attempts)
            return False, f"❌ Incorrect. Attempts left: **{remain}**."

        # Success → remove GATED, add MEMBER
        roles_cfg = cfg.get("roles", {})
        gated = _resolve_role_flex(member.guild, roles_cfg.get("gated"))
        member_role = _resolve_role_flex(member.guild, roles_cfg.get("member"))

        if gated and _can_manage_role(member.guild, gated) and gated in member.roles:
            try: await member.remove_roles(gated, reason="AgeGate verified — remove GATED")
            except Exception: pass
        if member_role and _can_manage_role(member.guild, member_role) and member_role not in member.roles:
            try: await member.add_roles(member_role, reason="AgeGate verified — grant Member")
            except Exception: pass

        self._challenges.pop(member.id, None)
        return True, "✅ Verified. Welcome!"

    # Events
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Give GATED on join if configured."""
        cfg = age_cfg(self.bot)
        if not cfg.get("enabled", True):
            return
        gated = _resolve_role_flex(member.guild, cfg.get("roles", {}).get("gated"))
        if gated and _can_manage_role(member.guild, gated) and gated not in member.roles:
            try: await member.add_roles(gated, reason="AgeGate autorole (GATED)")
            except Exception: pass

    # Cleanup loop
    @tasks.loop(minutes=5)
    async def _sweeper(self):
        for uid, ch in list(self._challenges.items()):
            if ch.expired():
                self._challenges.pop(uid, None)

    @_sweeper.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # Admin: publish/update the panel in the current channel
    @commands.has_permissions(administrator=True)
    @commands.command(name="welcome")
    async def publish_panel(self, ctx: commands.Context):
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.reply("❌ Run in a text channel.")
        emb = self._panel_embed(ctx.guild)
        view = WelcomePanelView(self)
        # update existing panel in this channel, if present
        target: Optional[discord.Message] = None
        try:
            async for m in ctx.channel.history(limit=50):
                if m.author.id == (ctx.bot.user.id if ctx.bot.user else 0) and m.components:
                    for row in m.components:
                        for c in getattr(row, "children", []):
                            if getattr(c, "custom_id", "") == VERIFY_BTN_ID:
                                target = m; break
                if target: break
        except Exception:
            pass
        if target:
            await target.edit(embed=emb, view=view)
            await ctx.reply("✅ Updated Age Check panel here.")
        else:
            await ctx.send(embed=emb, view=view)
            await ctx.reply("✅ Published Age Check panel here.")

async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeGate(bot))
