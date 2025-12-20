# Feral_Kitty_FiFi/features/welcome_gate.py
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

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
        "staff_names": ["Staff", "Security"],   # looked up by name and/or ID supported below
        "staff_ids": []                         # optional concrete IDs
    },
    "ids": {
        "ticket_category_id": 1400849393652990083,             # required for under-age tickets
        "log_channel_id": 1451663490308771981                  # optional but recommended
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

def _staff_roles(guild: discord.Guild, cfg: Dict[str, Any]) -> list[discord.Role]:
    out: list[discord.Role] = []
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
                # jail + open ticket
                await interaction.response.send_message(
                    f"🚫 You must be **{min_age}+**. You’ve been placed in **jail** and a verification ticket is being opened.",
                    ephemeral=True,
                )
                ok, msg = await self.cog._jail_and_open_ticket(member)
                await _log(self.cog.bot, guild, f"{'✅' if ok else '❌'} Underage flow for {member.mention}: {msg}")
                if not ok:
                    # show reason to the user too
                    try:
                        await interaction.followup.send(f"⚠️ Ticket not created: {msg}", ephemeral=True)
                    except Exception:
                        pass
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
    """Welcome panel → DOB modal → Under-age: jail + ticket; Adult: passcode → roles swap."""

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

    async def _finalize_passcode(self, member: discord.Member, user_code: str) -> tuple[bool, str]:
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
        cat = resolve_channel_any(member.guild, cat_id) if cat_id else None
        if not isinstance(cat, discord.CategoryChannel):
            return None
        # search in that category for a channel name containing user's last-4 and user id; simpler: by permissions
        for ch in cat.text_channels:
            ow = ch.overwrites_for(member)
            if ow.view_channel:
                return ch
        return None

    async def _jail_and_open_ticket(self, member: discord.Member) -> tuple[bool, str]:
        cfg = _wg_cfg(self.bot)
        ids = cfg.get("ids") or {}
        roles_cfg = cfg.get("roles") or {}
        guild = member.guild

        # roles: jail + strip manageable
        jailed = _find_role_by_name_or_id(guild, roles_cfg.get("jailed"))
        try:
            to_remove = [r for r in member.roles if not r.is_default() and can_manage_role(guild, r)]
            if to_remove:
                try: await member.remove_roles(*to_remove, reason="Under-age → jail")
                except Exception: pass
            if jailed and can_manage_role(guild, jailed) and jailed not in member.roles:
                try: await member.add_roles(jailed, reason="Under-age → jail")
                except Exception: pass
        except Exception as e:
            return False, f"role ops failed: {type(e).__name__}"

        # existing ticket?
        existing = await self._find_existing_ticket_for(member)
        if existing:
            try:
                await existing.send(f"{member.mention} returned to verification.", allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            except Exception:
                pass
            return True, f"ticket exists: {existing.mention}"

        # create ticket channel (with smart fallback)
        cat_id = (ids or {}).get("ticket_category_id")
        parent = resolve_channel_any(guild, cat_id) if cat_id else None

        # If a text-channel ID was pasted by mistake, use its category
        if isinstance(parent, discord.TextChannel) and parent.category:
            parent = parent.category

        # Fallback: infer from tickets.panel_options (prefer verification options)
        if not isinstance(parent, discord.CategoryChannel):
            tcfg = (self.bot.config or {}).get("tickets") or {}
            opts = tcfg.get("panel_options") or []

            for key in ("id_verification", "video_verification", "cross_verification"):
                opt = next((o for o in opts if str(o.get("value", "")).lower() == key), None)
                if opt and opt.get("parent_category_id"):
                    cand = resolve_channel_any(guild, opt["parent_category_id"])
                    if isinstance(cand, discord.CategoryChannel):
                        parent = cand
                        break

            if not isinstance(parent, discord.CategoryChannel):
                for o in opts:
                    pid = o.get("parent_category_id")
                    if pid:
                        cand = resolve_channel_any(guild, pid)
                        if isinstance(cand, discord.CategoryChannel):
                            parent = cand
                            break

            # Persist the inferred category so we don’t have to infer next time
            if isinstance(parent, discord.CategoryChannel) and not ids.get("ticket_category_id"):
                try:
                    cfg.setdefault("ids", {})["ticket_category_id"] = parent.id
                    await save_config(self.bot.config)
                except Exception:
                    pass

        if not isinstance(parent, discord.CategoryChannel):
            return False, f"ticket category not configured/invalid (id={cat_id})"

        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False, read_message_history=False)

        # opener perms
        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=True, attach_files=True
        )

        # staff perms
        for r in _staff_roles(guild, cfg):
            overwrites[r] = discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=True, attach_files=True, manage_messages=True
            )

        base = f"id-verify-{_slug_username(member)}"
        name = base; i = 1
        while discord.utils.get(guild.text_channels, name=name):
            i += 1; name = f"{base}-{i}"

        try:
            ch = await guild.create_text_channel(
                name=name, category=parent, overwrites=overwrites or None,
                reason=f"Under-age verification: {member} ({member.id})"
            )
        except discord.Forbidden:
            return False, "no permission to create channel"
        except discord.HTTPException as e:
            return False, f"HTTP error: {e}"

        # ping staff + intro + controls
        staff_mentions = " ".join([r.mention for r in _staff_roles(guild, cfg)])
        intro = discord.Embed(
            title="ID Verification Required",
            description=(
                f"{member.mention}, to remain in the server you must complete **ID Verification**.\n"
                "• Upload a clear photo of your **government ID** and a **note** with today’s date and your Discord tag.\n"
                "• Cover non-essential info.\n"
                "• A moderator will review and respond here."
            ),
            color=discord.Color.orange(), timestamp=utcnow()
        )
        try:
            await ch.send(content=(staff_mentions or None), embed=intro,
                          allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False))
            await ch.send(view=TicketCloseView(self))
        except Exception:
            pass

        # log
        await _log(self.bot, guild, f"🎫 Created under-age verification ticket {ch.mention} for {member.mention}.")
        return True, f"created: {ch.mention}"

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
