# Feral_Kitty_FiFi/features/welcome_gate.py
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional, List, Tuple

import discord
from discord.ext import commands, tasks

from ..config import save_config  # uses your existing loader/saver
from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any
from ..utils.perms import can_manage_role  # your existing helper (role < bot.top_role & not managed)


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
        return date(y, m, d)
    except Exception:
        return None

def _calc_age(dob: date, today: Optional[date] = None) -> int:
    today = today or utcnow().date()
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return years


# ===== config access =====
DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,
    "min_age": 18,
    "panel": {
        "title": "Welcome!",
        "description": (
            "Please begin verification by pressing **Age Check** below.\n\n"
            "❗ **Warning:** The DOB you provide must match your ID during verification."
        ),
        "image_url": ""
    },
    "passcode": {
        "timeout_hours": 48,
        "max_attempts": 4,
        "title": "Your Passcode",
        "description": (
            "Use **Enter Passcode** to submit this code.\n"
            "**Code:** `{code}`\n"
            "Expires in **{timeout_h}h**."
        ),
        "image_url": ""
    },
    "roles": {
        "gated": "GATED",
        "member": "Member",
        "jailed": "jailed",
        "staff_names": ["Staff", "Security"],
        "staff_ids": []
    },
    "ids": {
        # Under-age verification ticket destination (Category ID). If missing/invalid, we will infer from tickets.panel_options.
        "ticket_category_id": 1400849393652990083,
        # Log channel for audit entries
        "log_channel_id": 1451663490308771981,
        # Channel where the under-age prompt with buttons should be posted
        "fail_prompt_channel_id": 1438582972545503233
    },
    # Behavior toggles for under-age flow
    "fail_behavior": {
        "dm_user": True,                # DM the user on failure with 24h notice
        "ticket_ping_staff": True       # When a ticket is created via the buttons, ping staff roles in that ticket
    },
    "button_custom_id": "welcome_gate:age_check"
}

def _wg_cfg(bot: commands.Bot) -> Dict[str, Any]:
    cfg = bot.config.setdefault("welcome_gate2", {})
    # deep-merge defaults
    def merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict):
                merge(dst.setdefault(k, {}), v)
            else:
                dst.setdefault(k, v)
    merge(cfg, DEFAULT_CFG)
    return cfg

def _find_role_by_name_or_id(guild: discord.Guild, token: Any) -> Optional[discord.Role]:
    if token is None:
        return None
    if isinstance(token, int):
        return guild.get_role(token)
    s = str(token).strip()
    # mention
    if s.startswith("<@&") and s.endswith(">"):
        try:
            return guild.get_role(int(s[3:-1]))
        except Exception:
            return None
    # id
    try:
        rid = int(s)
        r = guild.get_role(rid)
        if r:
            return r
    except Exception:
        pass
    # by name (case-insensitive)
    for r in guild.roles:
        if r.name.lower() == s.lower():
            return r
    return None

def _staff_roles(guild: discord.Guild, cfg: Dict[str, Any]) -> List[discord.Role]:
    out: List[discord.Role] = []
    for tok in (cfg.get("roles", {}).get("staff_ids") or []):
        r = _find_role_by_name_or_id(guild, tok)
        if r: out.append(r)
    for name in (cfg.get("roles", {}).get("staff_names") or []):
        r = _find_role_by_name_or_id(guild, name)
        if r and r not in out:
            out.append(r)
    return out

async def _log(bot: commands.Bot, guild: discord.Guild, text: str):
    log_id = (_wg_cfg(bot).get("ids") or {}).get("log_channel_id")
    ch = resolve_channel_any(guild, log_id) if log_id else None
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass


# ===== runtime =====
@dataclass
class Challenge:
    user_id: int
    code: str
    expires_at: datetime
    attempts: int = 0

    def expired(self) -> bool:
        return utcnow() >= self.expires_at


# ===== views & modals =====
class WelcomePanelView(discord.ui.View):
    """Persistent button attached to the welcome panel message."""
    def __init__(self, cog: "WelcomeGate"):
        super().__init__(timeout=None)
        self.cog = cog
        cid = _wg_cfg(cog.bot).get("button_custom_id") or "welcome_gate:age_check"
        btn = discord.ui.Button(label="Age Check", style=discord.ButtonStyle.primary, custom_id=cid)
        btn.callback = self._on_clicked
        self.add_item(btn)

    async def _on_clicked(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)

        # If an under-age ticket already exists for this user, point them to it
        existing = await self.cog._find_existing_ticket_for(interaction.user)
        if existing:
            return await interaction.response.send_message(f"📨 You already have a verification ticket: {existing.mention}", ephemeral=True)

        await interaction.response.send_modal(AgeModal(self.cog, interaction.guild.id, interaction.user.id))


class UnderageVerifyPromptView(discord.ui.View):
    """Shown in a staff/user-visible channel; lets the flagged user open the right ticket."""
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id

    async def _open_ticket(self, interaction: discord.Interaction, value: str):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This button isn’t for you.", ephemeral=True)
        guild = interaction.guild or self.cog.bot.get_guild(self.guild_id)
        member = guild.get_member(self.user_id) if guild else None
        if not (guild and member):
            return await interaction.response.send_message("❌ Context missing.", ephemeral=True)

        ok, msg = await self.cog._open_ticket_for(member, value)
        if ok:
            await interaction.response.send_message(f"✅ Opened: {msg}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

    @discord.ui.button(label="Open ID Verify Ticket", style=discord.ButtonStyle.primary, emoji="🪪")
    async def idv_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._open_ticket(interaction, "id_verification")

    @discord.ui.button(label="Open VC Verify Ticket", style=discord.ButtonStyle.secondary, emoji="🎥")
    async def vc_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._open_ticket(interaction, "video_verification")


class AgeModal(discord.ui.Modal):
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int):
        super().__init__(title="Age Check", timeout=180)
        self.cog = cog; self.guild_id = guild_id; self.user_id = user_id
        self.dob = discord.ui.TextInput(label="Date of Birth (YYYY-MM-DD)", placeholder="2007-01-23", required=True, max_length=10)
        self.add_item(self.dob)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild or self.cog.bot.get_guild(self.guild_id)
            if not guild:
                return await interaction.response.send_message("❌ Missing guild.", ephemeral=True)
            member = guild.get_member(self.user_id) or await guild.fetch_member(self.user_id)
            if not member:
                return await interaction.response.send_message("❌ Member not found.", ephemeral=True)

            dob = _parse_yyyy_mm_dd(self.dob.value)
            if not dob:
                return await interaction.response.send_message("❌ DOB must be YYYY-MM-DD.", ephemeral=True)

            # log the DOB (your policy requires this)
            await self.cog._log_age_check_embed(guild, member, dob)

            min_age = int(_wg_cfg(self.cog.bot).get("min_age", 18))
            age = _calc_age(dob)
            if age < min_age:
                # jail (no auto ticket)
                ok, _ = await self.cog._jail_user(member)

                # DM user if enabled
                fb = (_wg_cfg(self.cog.bot).get("fail_behavior") or {})
                if fb.get("dm_user", True):
                    try:
                        dm = await member.create_dm()
                        await dm.send(
                            f"🚫 You must be **{min_age}+** to access the server.\n"
                            f"You’ve been placed in **jail**. You have **24 hours** to complete verification.\n\n"
                            f"Go back to the server and press one of the verification buttons in the designated channel."
                        )
                    except Exception:
                        pass

                # Ephemeral confirmation
                await interaction.response.send_message(
                    "You’ve been placed in **jail**. You have **24 hours** to complete ID verification. "
                    "A message with buttons has been posted in the verification channel.",
                    ephemeral=True,
                )

                # Post prompt with buttons in the specified channel
                ids = (_wg_cfg(self.cog.bot).get("ids") or {})
                prompt_channel_id = ids.get("fail_prompt_channel_id")
                prompt_ch = resolve_channel_any(guild, prompt_channel_id) if prompt_channel_id else None
                if isinstance(prompt_ch, discord.TextChannel):
                    try:
                        info = discord.Embed(
                            title="Verification Required",
                            description=(
                                f"{member.mention}, you have **24 hours** to complete verification.\n"
                                "Choose **ID VERIFY** for document review or **VC VERIFY** for a quick video verification.\n\n"
                                "_Only you can press these buttons._"
                            ),
                            color=discord.Color.orange(),
                            timestamp=datetime.now(timezone.utc),
                        )
                        await prompt_ch.send(
                            embed=info,
                            view=UnderageVerifyPromptView(self.cog, guild.id, member.id),
                            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                        )
                        await _log(self.cog.bot, guild, f"🧭 Underage prompt posted for {member.mention} in {prompt_ch.mention}")
                    except Exception:
                        pass
                else:
                    await _log(self.cog.bot, guild, "❌ Underage prompt channel not found/visible.")

                return

            # adult → passcode
            code = self.cog._start_or_refresh_challenge(member)
            embed = self.cog._passcode_embed(guild, code)
            view = PasscodePromptView(self.cog, guild.id, member.id)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            await _log(self.cog.bot, guild, f"🔐 Passcode issued to {member.mention}.")
        except Exception as e:
            await _log(self.cog.bot, interaction.guild, f"❌ AgeModal error: {type(e).__name__}: {e}")
            try:
                await interaction.response.send_message("❌ Something went wrong. Staff has been notified.", ephemeral=True)
            except Exception:
                pass


class PasscodePromptView(discord.ui.View):
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int):
        super().__init__(timeout=300)
        self.cog = cog; self.guild_id = guild_id; self.user_id = user_id
        btn = discord.ui.Button(label="Enter Passcode", style=discord.ButtonStyle.success)
        btn.callback = self._open
        self.add_item(btn)

    async def _open(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PasscodeModal(self.cog, self.guild_id, self.user_id))


class PasscodeModal(discord.ui.Modal):
    def __init__(self, cog: "WelcomeGate", guild_id: int, user_id: int):
        super().__init__(title="Enter Passcode", timeout=120)
        self.cog = cog; self.guild_id = guild_id; self.user_id = user_id
        self.code = discord.ui.TextInput(label="6-digit Passcode", placeholder="000000", required=True, max_length=12)
        self.add_item(self.code)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild or self.cog.bot.get_guild(self.guild_id)
        member = guild.get_member(self.user_id) if guild else None
        if not (guild and member):
            return await interaction.response.send_message("❌ Context missing.", ephemeral=True)

        ok, msg = await self.cog._finalize_passcode(member, self.code.value)
        await interaction.response.send_message(msg, ephemeral=True)
        await _log(self.cog.bot, guild, f"{'✅' if ok else '❌'} Passcode result for {member.mention}: {msg}")


class TicketCloseView(discord.ui.View):
    def __init__(self, cog: "WelcomeGate"):
        super().__init__(timeout=0)
        self.cog = cog

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒")
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Not a text channel.", ephemeral=True)
        # staff check: any configured staff role or Manage Channels
        cfg = _wg_cfg(self.cog.bot)
        staff = _staff_roles(interaction.guild, cfg)
        allowed = interaction.user.guild_permissions.manage_channels or any(r in interaction.user.roles for r in staff)
        if not allowed:
            return await interaction.response.send_message("❌ You cannot close tickets.", ephemeral=True)
        await interaction.response.send_message("Archiving…", ephemeral=True)
        await self.cog._archive_ticket_channel(interaction.channel, reason=f"Closed by {interaction.user}")


# ===== cog =====
class WelcomeGate(commands.Cog):
    """Welcome panel → DOB modal → Under-age: jail + prompt; Adult: passcode → roles swap."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._challenges: Dict[int, Challenge] = {}  # user_id -> Challenge
        self._sweeper.start()
        # register persistent button on load
        self.bot.add_view(WelcomePanelView(self))
        if not getattr(self.bot, "intents", None) or not self.bot.intents.members:
            print("[WelcomeGate] WARNING: Intents.members disabled; on_member_join won’t fire.")

    def cog_unload(self):
        self._sweeper.cancel()

    # ---- panels / embeds ----
    def _panel_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = _wg_cfg(self.bot)
        p = cfg.get("panel", {})
        emb = discord.Embed(title=p.get("title") or "Welcome!", description=p.get("description") or "", color=discord.Color.blurple(), timestamp=utcnow())
        if p.get("image_url"):
            emb.set_image(url=p["image_url"])
        return emb

    def _passcode_embed(self, guild: discord.Guild, code: str) -> discord.Embed:
        cfg = _wg_cfg(self.bot)
        pc = cfg.get("passcode", {})
        desc = (pc.get("description") or "").replace("{code}", code).replace("{timeout_h}", str(int(pc.get("timeout_hours") or 48)))
        emb = discord.Embed(title=pc.get("title") or "Your Passcode", description=desc, color=discord.Color.green(), timestamp=utcnow())
        if pc.get("image_url"):
            emb.set_image(url=pc["image_url"])
        return emb

    async def _log_age_check_embed(self, guild: discord.Guild, member: discord.Member, dob: date):
        cfg = _wg_cfg(self.bot)
        log_id = (cfg.get("ids") or {}).get("log_channel_id")
        ch = resolve_channel_any(guild, log_id) if log_id else None
        if not isinstance(ch, discord.TextChannel):
            return
        emb = discord.Embed(title="Age Check Submitted", color=discord.Color.blurple(), timestamp=utcnow())
        emb.add_field(name="User", value=f"{member} ({member.mention})", inline=False)
        emb.add_field(name="User ID", value=str(member.id), inline=True)
        emb.add_field(name="DOB Entered", value=dob.isoformat(), inline=True)
        emb.set_thumbnail(url=member.display_avatar.url if member.display_avatar else discord.Embed.Empty)
        try:
            await ch.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    # ---- challenges ----
    def _gen_code(self) -> str:
        return f"{random.randint(0, 999_999):06d}"

    def _start_or_refresh_challenge(self, member: discord.Member) -> str:
        cfg = _wg_cfg(self.bot)
        timeout_h = int((cfg.get("passcode") or {}).get("timeout_hours") or 48)
        code = self._gen_code()
        self._challenges[member.id] = Challenge(
            user_id=member.id,
            code=code,
            expires_at=utcnow() + timedelta(hours=timeout_h),
            attempts=0,
        )
        return code

    async def _finalize_passcode(self, member: discord.Member, user_code: str) -> Tuple[bool, str]:
        cfg = _wg_cfg(self.bot)
        pc = cfg.get("passcode") or {}
        max_attempts = int(pc.get("max_attempts") or 4)

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

        # success → roles swap
        roles_cfg = cfg.get("roles") or {}
        gated = _find_role_by_name_or_id(member.guild, roles_cfg.get("gated"))
        member_role = _find_role_by_name_or_id(member.guild, roles_cfg.get("member"))

        if gated and can_manage_role(member.guild, gated) and gated in member.roles:
            try: await member.remove_roles(gated, reason="WelcomeGate verified — remove gated")
            except Exception: pass

        if member_role and can_manage_role(member.guild, member_role) and member_role not in member.roles:
            try: await member.add_roles(member_role, reason="WelcomeGate verified — grant member")
            except Exception: pass

        self._challenges.pop(member.id, None)
        return True, "✅ Verified. Welcome!"

    # ---- tickets (under-age) ----
    async def _find_existing_ticket_for(self, member: discord.Member) -> Optional[discord.TextChannel]:
        cat_id = (_wg_cfg(self.bot).get("ids") or {}).get("ticket_category_id")
        # Use native resolver first
        cat = None
        if cat_id:
            cat = member.guild.get_channel(cat_id) or self.bot.get_channel(cat_id)
        if not isinstance(cat, discord.CategoryChannel) and cat_id:
            obj = resolve_channel_any(member.guild, cat_id)
            if isinstance(obj, discord.CategoryChannel):
                cat = obj
        if not isinstance(cat, discord.CategoryChannel):
            return None
        for ch in cat.text_channels:
            ow = ch.overwrites_for(member)
            if ow.view_channel:
                return ch
        return None

    async def _jail_user(self, member: discord.Member) -> Tuple[bool, str]:
        """Jail the member and strip manageable roles; no ticket creation."""
        cfg = _wg_cfg(self.bot)
        roles_cfg = cfg.get("roles") or {}
        guild = member.guild
        jailed = _find_role_by_name_or_id(guild, roles_cfg.get("jailed"))
        try:
            to_remove = [r for r in member.roles if not r.is_default() and can_manage_role(guild, r)]
            if to_remove:
                try: await member.remove_roles(*to_remove, reason="Under-age → jail")
                except Exception: pass
            if jailed and can_manage_role(guild, jailed) and jailed not in member.roles:
                try: await member.add_roles(jailed, reason="Under-age → jail")
                except Exception: pass
            return True, "jailed"
        except Exception as e:
            return False, f"role ops failed: {type(e).__name__}"

    async def _open_ticket_for(self, member: discord.Member, value: str) -> Tuple[bool, str]:
        """Create a ticket channel for the given option value ('id_verification' or 'video_verification')."""
        guild = member.guild
        tickets_cfg = (self.bot.config or {}).get("tickets") or {}
        opts = tickets_cfg.get("panel_options") or []

        opt = next((o for o in opts if str(o.get("value", "")).lower() == str(value).lower()), None)
        if not opt:
            return False, "ticket option not available"

        # resolve category: prefer the option’s parent_category_id; fallback to welcome_gate2.ids.ticket_category_id
        parent = None
        pid = opt.get("parent_category_id")
        if pid:
            parent = guild.get_channel(pid) or self.bot.get_channel(pid)
            if isinstance(parent, (discord.TextChannel, discord.Thread)) and getattr(parent, "category", None):
                parent = parent.category
        if not isinstance(parent, discord.CategoryChannel):
            wg_ids = (_wg_cfg(self.bot).get("ids") or {})
            alt = wg_ids.get("ticket_category_id")
            if alt:
                parent = guild.get_channel(alt) or self.bot.get_channel(alt)
                if isinstance(parent, (discord.TextChannel, discord.Thread)) and getattr(parent, "category", None):
                    parent = parent.category
        if not isinstance(parent, discord.CategoryChannel):
            return False, "ticket category not configured/invalid"

        # resolve staff roles (IDs preferred, fallback to names)
        staff_ids: List[int] = []
        opt_ids = [int(x) for x in (opt.get("staff_role_ids") or []) if isinstance(x, int) or str(x).isdigit()]
        opt_ids = [i for i in opt_ids if guild.get_role(i)]
        if opt_ids:
            staff_ids = sorted(set(opt_ids))
        else:
            ids = [int(x) for x in (tickets_cfg.get("staff_role_ids") or []) if isinstance(x, int) or str(x).isdigit()]
            ids = [i for i in ids if guild.get_role(i)]
            if ids:
                staff_ids = sorted(set(ids))
            else:
                names = tickets_cfg.get("roles_to_ping_names") or []
                for n in names:
                    r = resolve_role_any(guild, n)
                    if r: staff_ids.append(r.id)
                staff_ids = sorted(set(staff_ids))

        # channel name like panel: YYYYMM-last4-#### (per-category counter)
        def yyyymm(dt=None):
            dt = dt or datetime.now(timezone.utc)
            return f"{dt.year:04d}{dt.month:02d}"
        counters = tickets_cfg.setdefault("counters", {}).setdefault(yyyymm(), {})
        seq = int(counters.get(value, 1)); counters[value] = seq + 1
        base = f"{yyyymm()}-{member.id % 10000:04d}-{seq:04d}"

        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False, read_message_history=False)
        overwrites[member] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=True, attach_files=True, embed_links=True)
        for rid in staff_ids:
            r = guild.get_role(rid)
            if r:
                overwrites[r] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=True, attach_files=True, manage_messages=True, embed_links=True)

        # create text channel
        try:
            text_ch = await guild.create_text_channel(
                name=base, category=parent, overwrites=overwrites or None,
                reason=f"Ticket opened by {member} ({value})"
            )
        except discord.Forbidden:
            return False, "I lack permission to create channels"
        except discord.HTTPException as e:
            return False, f"HTTP error creating channel: {e}"

        # optional VC
        voice_ch = None
        if bool(opt.get("open_voice", False)):
            v_ow: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
            v_ow[guild.default_role] = discord.PermissionOverwrite(connect=False, view_channel=False)
            v_ow[member] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True)
            for rid in staff_ids:
                r = guild.get_role(rid)
                if r:
                    v_ow[r] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True)
            try:
                voice_ch = await guild.create_voice_channel(
                    name=f"{base}-vc"[:100], category=parent, overwrites=v_ow or None, reason=f"Ticket voice opened by {member} ({value})"
                )
            except Exception:
                voice_ch = None  # non-fatal

        # intro + ping (controlled by fail_behavior.ticket_ping_staff) with explicit AllowedMentions
        fb = (_wg_cfg(self.bot).get("fail_behavior") or {})
        ping_staff = bool(fb.get("ticket_ping_staff", True))

        intro = discord.Embed(
            title=f"Ticket: {opt.get('label') or value}",
            description=(
                f"Opened by {member.mention} • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n\n"
                + ("🎥 **Voice channel created:** " + (voice_ch.mention if voice_ch else "_failed_") + "\n\n" if opt.get("open_voice") else "")
                + "Provide details below."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        try:
            if ping_staff and staff_ids:
                content = " ".join(f"<@&{rid}>" for rid in staff_ids)
                allowed = discord.AllowedMentions(
                    roles=[discord.Object(id=rid) for rid in staff_ids],
                    users=True,
                    everyone=False,
                )
                await text_ch.send(content=content, embed=intro, allowed_mentions=allowed)
            else:
                await text_ch.send(embed=intro)
        except Exception:
            pass

        # record basic metadata (optional, mirrors your tickets schema)
        tickets_cfg.setdefault("records", []).append({
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "category": value,
            "opener_name": str(member),
            "opener_id": member.id,
            "channel_id": text_ch.id,
            "voice_channel_id": voice_ch.id if voice_ch else None,
            "claimed_by_name": "",
            "claimed_by_id": None,
            "claimed_at": "",
            "closed_at": "",
            "transcript_msg_url": "",
            "transcript_cdn_url": "",
            "archived": True,
        })
        tickets_cfg.setdefault("active", {})[str(text_ch.id)] = {
            "opener_id": member.id,
            "value": value,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "claimed_by": None,
            "claimed_at": None,
            "verification": bool(opt.get("verification", False)),
            "voice_channel_id": voice_ch.id if voice_ch else None,
        }
        try:
            await save_config(self.bot.config)
        except Exception:
            pass

        return True, text_ch.mention

    async def _archive_ticket_channel(self, channel: discord.TextChannel, reason: str = "Closed"):
        try:
            overwrites = channel.overwrites
            for target, pw in list(overwrites.items()):
                pw.send_messages = False
                overwrites[target] = pw
            await channel.edit(name=f"{channel.name}-closed", overwrites=overwrites, reason=reason)
        except Exception:
            pass
        await _log(self.bot, channel.guild, f"📦 Archived ticket {channel.mention}: {reason}")

    # ---- events ----
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = _wg_cfg(self.bot)
        roles_cfg = cfg.get("roles") or {}
        gated = _find_role_by_name_or_id(member.guild, roles_cfg.get("gated"))
        if gated and can_manage_role(member.guild, gated):
            try: await member.add_roles(gated, reason="WelcomeGate autorole (gated)")
            except Exception: pass

    # background cleanup
    @tasks.loop(minutes=5)
    async def _sweeper(self):
        expired = [uid for uid, ch in list(self._challenges.items()) if ch.expired()]
        for uid in expired:
            self._challenges.pop(uid, None)

    @_sweeper.before_loop
    async def _before_sweeper(self):
        await self.bot.wait_until_ready()

    # ---- admin: publish panel ----
    @commands.has_permissions(administrator=True)
    @commands.command(name="welcome")  # run in the channel where you want the panel
    async def publish_panel(self, ctx: commands.Context):
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.reply("❌ Run this in a text channel.")
        embed = self._panel_embed(ctx.guild)
        view = WelcomePanelView(self)

        # try to update any existing panel with our custom_id
        cid = _wg_cfg(self.bot).get("button_custom_id") or "welcome_gate:age_check"
        target: Optional[discord.Message] = None
        try:
            async for m in ctx.channel.history(limit=50):
                if m.author.id == ctx.bot.user.id and m.components:
                    if any(getattr(c, "custom_id", None) == cid for row in m.components for c in row.children):
                        target = m; break
        except Exception:
            target = None

        if target:
            await target.edit(embed=embed, view=view)
            await ctx.reply("✅ Updated Verify panel here.")
        else:
            await ctx.send(embed=embed, view=view)
            await ctx.reply("✅ Published Verify panel here.")


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeGate(bot))
