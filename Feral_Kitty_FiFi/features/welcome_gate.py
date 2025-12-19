# Feral_Kitty_FiFi/features/welcome_gate.py
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional, Any, List

import discord
from discord.ext import commands, tasks

from ..config import save_config  # if you later persist ticket state
from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any


# ===== helpers =====
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
        # small sanity guard on year
        if y < 1900 or y > utcnow().year:
            return None
        return date(y, m, d)
    except Exception:
        return None

def _calc_age(dob: date, today: Optional[date] = None) -> int:
    today = today or utcnow().date()
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return years

def _can_manage_role(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return bool(me) and role < me.top_role and not role.is_default() and not role.managed

# config access
def _cfg(bot: commands.Bot) -> Dict[str, Any]:
    return bot.config.setdefault("age_gate", {})  # all keys provided in data/config.json

def _cfg_int(dct: Dict[str, Any], key: str, default: int) -> int:
    val = dct.get(key, default)
    try:
        return int(val)
    except Exception:
        return default

def _resolve_role_cfg(guild: discord.Guild, entry: Dict[str, Any]) -> Optional[discord.Role]:
    """entry is like {"id": 123} or {"name": "Staff"}."""
    if not entry: return None
    rid = entry.get("id")
    if isinstance(rid, int):
        r = guild.get_role(rid)
        if r: return r
    name = entry.get("name")
    if isinstance(name, str) and name.strip():
        return resolve_role_any(guild, name.strip())
    return None

def _resolve_ping_mentions(guild: discord.Guild, tokens: List[Any]) -> List[str]:
    """mix of ids/names; returns role mentions that exist."""
    out = []
    for tok in tokens or []:
        r = resolve_role_any(guild, tok)
        if r: out.append(r.mention)
    return out


# ===== runtime containers =====
@dataclass
class Challenge:
    user_id: int
    code: str
    expires_at: datetime
    attempts: int = 0

    def expired(self) -> bool:
        return utcnow() >= self.expires_at


# ===== UI components =====
class WelcomePanelView(discord.ui.View):
    """Persistent 'Age Check' button; used on the public panel in the GATED channel."""
    def __init__(self, cog: "WelcomeGate"):
        super().__init__(timeout=None)
        self.cog = cog
        custom_id = _cfg(self.cog.bot).get("verify_button_custom_id") or "welcome_gate:age_check"
        btn = discord.ui.Button(label="Age Check", style=discord.ButtonStyle.primary, custom_id=custom_id)
        btn.callback = self._on_age_check_clicked
        self.add_item(btn)

    async def _on_age_check_clicked(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)
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

        # Immutable log (DOB is intentionally logged by policy)
        await self.cog._log_age_check_embed(guild, member, dob)

        min_age = _cfg_int(_cfg(self.cog.bot), "min_age", 18)
        age = _calc_age(dob)
        if age < min_age:
            await interaction.response.send_message(
                f"🚫 You must be **{min_age}+**. You have been placed in **jail** and an **ID verification ticket** was opened.",
                ephemeral=True,
            )
            await self.cog._jail_and_open_ticket(member)
            await self.cog._log_simple(guild, f"🚨 Underage ({age}) → jailed & ticket for {member.mention}.")
            return

        # Adult → passcode modal flow (no channel creation)
        code = self.cog._start_or_refresh_challenge(member)
        embed = self.cog._passcode_embed(guild, code)
        view = PasscodePromptView(self.cog, guild.id, member.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await self.cog._log_simple(guild, f"🔐 Passcode issued to {member.mention}.")


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
        await self.cog._log_simple(guild, f"{'✅' if ok else '❌'} Passcode result for {member.mention}: {msg}")


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
        cfg = _cfg(self.cog.bot)
        roles_cfg = cfg.get("roles", {})
        sec = _resolve_role_cfg(guild, roles_cfg.get("security", {}))
        staff = _resolve_role_cfg(guild, roles_cfg.get("staff", {}))
        allowed = interaction.user.guild_permissions.manage_channels or any(
            (sec and sec in getattr(interaction.user, "roles", [])),
            ) or any((staff and staff in getattr(interaction.user, "roles", [])),)

        if not allowed:
            return await interaction.response.send_message("❌ You cannot close tickets.", ephemeral=True)
        await interaction.response.send_message("Archiving…", ephemeral=True)
        await self.cog._archive_ticket_channel(interaction.channel, reason=f"Closed by {interaction.user}")


# ===== Cog =====
class WelcomeGate(commands.Cog):
    """GATED channel panel → DOB modal → passcode (adult) OR jailed+ticket (underage)."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._challenges: Dict[int, Challenge] = {}   # user_id -> Challenge
        self._tickets: Dict[int, int] = {}            # user_id -> channel_id (in-memory; used only to ping on leave)
        self._used_codes: set[str] = set()            # basic non-reuse guard
        self._sweeper.start()
        self.bot.add_view(WelcomePanelView(self))     # persistent button
        if not getattr(self.bot, "intents", None) or not self.bot.intents.members:
            print("[WelcomeGate] WARNING: Intents.members disabled; on_member_join won’t fire.")

    def cog_unload(self):
        self._sweeper.cancel()

    # ----- Embeds / Logs -----
    def _panel_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = _cfg(self.bot)
        w = cfg.get("welcome_embed", {}) or {}
        emb = discord.Embed(
            title=(w.get("title") or "Welcome!"),
            description=(w.get("description") or "Press **Age Check** below."),
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        if w.get("image_url"):
            emb.set_image(url=w["image_url"])
        return emb

    def _passcode_embed(self, guild: discord.Guild, code: str) -> discord.Embed:
        cfg = _cfg(self.bot)
        p = cfg.get("passcode_embed", {}) or {}
        desc_tpl = (p.get("description") or "Your code: `{code}` (expires in {timeout_h}h)")
        desc = desc_tpl.replace("{code}", code).replace("{timeout_h}", str(_cfg_int(cfg, "passcode_timeout_hours", 48)))
        emb = discord.Embed(
            title=(p.get("title") or "Your Passcode"),
            description=desc,
            color=discord.Color.green(),
            timestamp=utcnow(),
        )
        if p.get("image_url"):
            emb.set_image(url=p["image_url"])
        return emb

    async def _log_simple(self, guild: discord.Guild, text: str):
        cfg = _cfg(self.bot)
        ch = resolve_channel_any(guild, cfg.get("log_channel_id"))
        if isinstance(ch, discord.TextChannel):
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def _log_age_check_embed(self, guild: discord.Guild, member: discord.Member, dob: date):
        cfg = _cfg(self.bot)
        ch = resolve_channel_any(guild, cfg.get("log_channel_id"))
        if not isinstance(ch, discord.TextChannel):
            return
        emb = discord.Embed(title="Age Check Submitted", color=discord.Color.blurple(), timestamp=utcnow())
        emb.add_field(name="User", value=f"{member} ({member.mention})", inline=False)
        emb.add_field(name="User ID", value=str(member.id), inline=True)
        # per your policy, log full DOB
        emb.add_field(name="DOB Entered", value=dob.isoformat(), inline=True)
        try:
            emb.set_thumbnail(url=member.display_avatar.url)  # modern attr
        except Exception:
            pass
        await ch.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())

    # ----- Challenge lifecycle -----
    def _gen_code(self) -> str:
        for _ in range(200):
            code = f"{random.randint(0, 999_999):06d}"
            if code not in self._used_codes and all(ch.code != code for ch in self._challenges.values()):
                return code
        return f"{random.randint(0, 999_999):06d}"

    def _start_or_refresh_challenge(self, member: discord.Member) -> str:
        cfg = _cfg(self.bot)
        timeout_h = _cfg_int(cfg, "passcode_timeout_hours", 48)
        code = self._gen_code()
        self._challenges[member.id] = Challenge(
            user_id=member.id,
            code=code,
            expires_at=utcnow() + timedelta(hours=timeout_h),
            attempts=0,
        )
        return code

    async def _finalize_passcode(self, member: discord.Member, user_code: str) -> tuple[bool, str]:
        guild = member.guild
        cfg = _cfg(self.bot)
        attempts_max = _cfg_int(cfg, "passcode_attempts", 4)

        ch = self._challenges.get(member.id)
        if not ch or ch.expired():
            self._challenges.pop(member.id, None)
            return False, "⏱️ Session expired. Press **Age Check** again."
        if ch.attempts >= attempts_max:
            self._challenges.pop(member.id, None)
            return False, "❌ Attempts exceeded. Contact staff."
        if (user_code or "").strip() != ch.code:
            ch.attempts += 1
            remain = attempts_max - ch.attempts
            return False, f"❌ Incorrect. Attempts left: **{remain}**."

        # Success → remove GATED, grant Member
        roles_cfg = cfg.get("roles", {}) or {}
        gated = _resolve_role_cfg(guild, roles_cfg.get("gated", {}))
        member_role = _resolve_role_cfg(guild, roles_cfg.get("member", {}))
        if gated and _can_manage_role(guild, gated) and gated in member.roles:
            try: await member.remove_roles(gated, reason="WelcomeGate verified — remove GATED")
            except Exception: pass
        if member_role and _can_manage_role(guild, member_role) and member_role not in member.roles:
            try: await member.add_roles(member_role, reason="WelcomeGate verified — grant Member")
            except Exception: pass

        self._used_codes.add(ch.code)
        self._challenges.pop(member.id, None)
        await self._log_simple(guild, f"✅ {member.mention} verified (passcode).")
        return True, "✅ Verified. Welcome!"

    # ----- Ticket helpers (underage only) -----
    async def _jail_and_open_ticket(self, member: discord.Member):
        guild = member.guild
        cfg = _cfg(self.bot)
        roles_cfg = cfg.get("roles", {}) or {}

        # strip manageable roles; add JAILED
        jailed = _resolve_role_cfg(guild, roles_cfg.get("jailed", {}))
        if jailed:
            to_remove = [r for r in member.roles if not r.is_default() and _can_manage_role(guild, r)]
            if to_remove:
                try: await member.remove_roles(*to_remove, reason="WelcomeGate age check failed → jail")
                except Exception: pass
            if _can_manage_role(guild, jailed) and jailed not in member.roles:
                try: await member.add_roles(jailed, reason="WelcomeGate age check failed → jail")
                except Exception: pass
        else:
            await self._log_simple(guild, f"⚠️ JAILED role not found; cannot jail {member.mention}.")

        # create private id-verify ticket under configured category
        cat = resolve_channel_any(guild, cfg.get("ticket_category_id"))
        if not isinstance(cat, discord.CategoryChannel):
            return await self._log_simple(guild, "⚠️ Ticket category missing or invalid.")

        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)

        sec = _resolve_role_cfg(guild, roles_cfg.get("security", {}))
        staff = _resolve_role_cfg(guild, roles_cfg.get("staff", {}))
        mod_pw = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True, manage_messages=True
        )
        if sec:   overwrites[sec]   = mod_pw
        if staff and staff != sec: overwrites[staff] = mod_pw

        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        )

        prefix = str(cfg.get("ticket_prefix") or "id-verify")
        base = f"{prefix}-{_slug_username(member)}"
        name = base; i = 1
        while discord.utils.get(guild.text_channels, name=name) is not None:
            i += 1; name = f"{base}-{i}"

        try:
            ch = await guild.create_text_channel(
                name=name, category=cat, overwrites=overwrites,
                reason=f"Age verification required for {member} ({member.id})"
            )
        except Exception:
            return

        # post intro + close button, ping roles
        pings = " ".join(_resolve_ping_mentions(guild, cfg.get("staff_ping_roles") or []))
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
        await self._log_simple(channel.guild, f"📦 Archived ticket {channel.mention}: {reason}")

    # ----- Events -----
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = _cfg(self.bot)
        if not cfg.get("enabled", True):
            return
        gated = _resolve_role_cfg(member.guild, (cfg.get("roles", {}) or {}).get("gated", {}))
        if gated and _can_manage_role(member.guild, gated):
            try: await member.add_roles(gated, reason="WelcomeGate autorole (GATED)")
            except Exception: pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """No auto-ban. We just notify staff if they had an open verification ticket."""
        guild = member.guild
        ch_id = self._tickets.pop(member.id, None)
        if ch_id:
            pings = " ".join(_resolve_ping_mentions(guild, _cfg(self.bot).get("staff_ping_roles") or []))
            await self._log_simple(guild, f"🚪 {member} left during verification. {pings}".strip())

    # ----- Background cleanup -----
    @tasks.loop(minutes=5)
    async def _sweeper(self):
        # expire challenges; keep small used_codes memory
        expired = [uid for uid, ch in list(self._challenges.items()) if ch.expired()]
        for uid in expired:
            self._used_codes.add(self._challenges[uid].code)
            self._challenges.pop(uid, None)
        if len(self._used_codes) > 10000:
            for _ in range(2500):
                try:
                    self._used_codes.pop()
                except KeyError:
                    break

    @_sweeper.before_loop
    async def _before_sweeper(self):
        await self.bot.wait_until_ready()

    # ----- Admin command: publish/update panel (run with !welcome) -----
    @commands.has_permissions(administrator=True)
    @commands.command(name="welcome")  # call with !welcome
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
                    # look for our custom_id
                    try:
                        for row in m.components:
                            for c in getattr(row, "children", []):
                                if getattr(c, "custom_id", None) == (_cfg(self.bot).get("verify_button_custom_id") or "welcome_gate:age_check"):
                                    msg_to_edit = m
                                    raise StopIteration
                    except StopIteration:
                        break
        except Exception:
            msg_to_edit = None

        if msg_to_edit:
            await msg_to_edit.edit(embed=embed, view=view)
            await ctx.reply("✅ Updated Verify panel here.")
        else:
            await ctx.send(embed=embed, view=view)
            await ctx.reply("✅ Published Verify panel here.")


# ===== extension entrypoint =====
async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeGate(bot))
