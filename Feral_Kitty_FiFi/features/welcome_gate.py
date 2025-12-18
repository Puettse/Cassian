# Feral_Kitty_FiFi/features/welcome_gate.py
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set

import discord
from discord.ext import commands, tasks

from ..config import save_config  # uses your existing save_config
from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any
from ..utils.perms import can_manage_role  # bot’s role hierarchy check


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


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
    "channel_id": None,          # welcome embed channel
    "dm_user": False,            # also DM the user
    "log_channel_id": None,      # moderation log channel
    "message": {
        "title": "Welcome!",
        "description": "Welcome to the server, {mention}.\nPlease read the instructions below.",
        "image_url": "",
    },
    "autorole_id": None,         # role to give on join (e.g., Gated)
    "challenge": {
        "enabled": True,
        "timeout_hours": 72,
        "max_attempts": 5,
        "remove_role_id": None,  # gated role to remove upon success
        "grant_role_id": None,   # replacement role to grant upon success
    },
}


class WelcomeConsoleView(discord.ui.View):
    """Admin console to configure welcome gate."""

    def __init__(self, cog: "WelcomeGate", ctx: commands.Context):
        super().__init__(timeout=600)
        self.cog = cog
        self.ctx = ctx

    async def _refresh(self, interaction: discord.Interaction):
        cfg = self.cog.cfg(self.ctx.guild.id)
        ch = resolve_channel_any(self.ctx.guild, cfg.get("channel_id"))
        log_ch = resolve_channel_any(self.ctx.guild, cfg.get("log_channel_id"))
        autorole = resolve_role_any(self.ctx.guild, cfg.get("autorole_id"))
        chall = (cfg.get("challenge") or {})
        gated = resolve_role_any(self.ctx.guild, chall.get("remove_role_id"))
        grant = resolve_role_any(self.ctx.guild, chall.get("grant_role_id"))

        embed = discord.Embed(
            title="Welcome Gate — Configuration",
            description="Use the buttons to edit settings. Values persist to `data/config.json`.",
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        embed.add_field(name="Enabled", value=str(bool(cfg.get("enabled", True))), inline=True)
        embed.add_field(name="Welcome Channel", value=(ch.mention if ch else str(cfg.get("channel_id"))), inline=True)
        embed.add_field(name="DM User", value=str(bool(cfg.get("dm_user", False))), inline=True)
        embed.add_field(name="Log Channel", value=(log_ch.mention if log_ch else str(cfg.get("log_channel_id"))), inline=True)
        embed.add_field(name="Autorole on Join", value=(autorole.mention if autorole else str(cfg.get("autorole_id"))), inline=True)
        m = cfg.get("message") or {}
        preview_title = m.get("title") or "Welcome!"
        preview_desc = (m.get("description") or "")[:180]
        embed.add_field(name="Message Title", value=preview_title, inline=True)
        embed.add_field(name="Message Preview", value=preview_desc or "_empty_", inline=False)
        if m.get("image_url"):
            embed.set_image(url=m["image_url"])
        embed.add_field(name="Challenge Enabled", value=str(bool(chall.get("enabled", True))), inline=True)
        embed.add_field(name="Timeout (h)", value=str(int(chall.get("timeout_hours") or 0)), inline=True)
        embed.add_field(name="Max Attempts", value=str(int(chall.get("max_attempts") or 0)), inline=True)
        embed.add_field(name="Gated Role (remove)", value=(gated.mention if gated else str(chall.get("remove_role_id"))), inline=True)
        embed.add_field(name="Replacement Role (grant)", value=(grant.mention if grant else str(chall.get("grant_role_id"))), inline=True)
        await interaction.message.edit(embed=embed, view=self)

    # Toggles / Setters

    @discord.ui.button(label="Toggle Enabled", style=discord.ButtonStyle.primary)
    async def btn_toggle_enabled(self, interaction: discord.Interaction, _: discord.ui.Button):
        cfg = self.cog.cfg(self.ctx.guild.id)
        cfg["enabled"] = not bool(cfg.get("enabled", True))
        await save_config(self.cog.bot.config)
        await interaction.response.send_message(f"Enabled → {cfg['enabled']}", ephemeral=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Set Welcome Channel", style=discord.ButtonStyle.secondary)
    async def btn_set_channel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Reply with a **channel mention**, **ID**, or **exact name** within 30s.", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for(
                "message",
                timeout=30.0,
                check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True)
            return
        ch = resolve_channel_any(self.ctx.guild, msg.content)
        if not isinstance(ch, discord.TextChannel):
            await interaction.followup.send("❌ Invalid text channel.", ephemeral=True)
            return
        self.cog.cfg(self.ctx.guild.id)["channel_id"] = ch.id
        await save_config(self.cog.bot.config)
        await interaction.followup.send(f"✅ Welcome channel set to {ch.mention}", ephemeral=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Toggle DM User", style=discord.ButtonStyle.secondary)
    async def btn_toggle_dm(self, interaction: discord.Interaction, _: discord.ui.Button):
        cfg = self.cog.cfg(self.ctx.guild.id)
        cfg["dm_user"] = not bool(cfg.get("dm_user", False))
        await save_config(self.cog.bot.config)
        await interaction.response.send_message(f"DM User → {cfg['dm_user']}", ephemeral=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Set Log Channel", style=discord.ButtonStyle.secondary)
    async def btn_set_log(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Reply with a **channel mention**, **ID**, or **exact name** within 30s.", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for(
                "message",
                timeout=30.0,
                check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True)
            return
        ch = resolve_channel_any(self.ctx.guild, msg.content)
        if not isinstance(ch, discord.TextChannel):
            await interaction.followup.send("❌ Invalid text channel.", ephemeral=True)
            return
        self.cog.cfg(self.ctx.guild.id)["log_channel_id"] = ch.id
        await save_config(self.cog.bot.config)
        await interaction.followup.send(f"✅ Log channel set to {ch.mention}", ephemeral=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Set Autorole", style=discord.ButtonStyle.secondary)
    async def btn_set_autorole(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Reply with **@Role**, **ID**, or **[Exact Name]** within 30s. Type `clear` to unset.", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for(
                "message",
                timeout=30.0,
                check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True)
            return
        if msg.content.strip().lower() == "clear":
            self.cog.cfg(self.ctx.guild.id)["autorole_id"] = None
        else:
            role = resolve_role_any(self.ctx.guild, msg.content)
            if not role:
                await interaction.followup.send("❌ Role not found.", ephemeral=True)
                return
            self.cog.cfg(self.ctx.guild.id)["autorole_id"] = role.id
        await save_config(self.cog.bot.config)
        await interaction.followup.send("✅ Updated autorole.", ephemeral=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Challenge: Toggle", style=discord.ButtonStyle.primary)
    async def btn_challenge_toggle(self, interaction: discord.Interaction, _: discord.ui.Button):
        chall = self.cog.cfg(self.ctx.guild.id).setdefault("challenge", {})
        chall["enabled"] = not bool(chall.get("enabled", True))
        await save_config(self.cog.bot.config)
        await interaction.response.send_message(f"Challenge Enabled → {chall['enabled']}", ephemeral=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Challenge: Timeout (h)", style=discord.ButtonStyle.secondary)
    async def btn_challenge_timeout(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Enter timeout in **hours** (1–72), within 30s.", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for(
                "message",
                timeout=30.0,
                check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True)
            return
        try:
            hours = clamp(int(msg.content.strip()), 1, 72)
        except ValueError:
            await interaction.followup.send("❌ Invalid number.", ephemeral=True)
            return
        self.cog.cfg(self.ctx.guild.id).setdefault("challenge", {})["timeout_hours"] = hours
        await save_config(self.cog.bot.config)
        await interaction.followup.send(f"✅ Timeout set to {hours}h.", ephemeral=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Challenge: Max Attempts", style=discord.ButtonStyle.secondary)
    async def btn_challenge_attempts(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Enter **max attempts** (1–10), within 30s.", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for(
                "message",
                timeout=30.0,
                check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True)
            return
        try:
            n = clamp(int(msg.content.strip()), 1, 10)
        except ValueError:
            await interaction.followup.send("❌ Invalid number.", ephemeral=True)
            return
        self.cog.cfg(self.ctx.guild.id).setdefault("challenge", {})["max_attempts"] = n
        await save_config(self.cog.bot.config)
        await interaction.followup.send(f"✅ Max attempts set to {n}.", ephemeral=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Challenge: Gated Role (remove)", style=discord.ButtonStyle.secondary)
    async def btn_challenge_gated(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Reply with **@Role**, **ID**, **[Exact Name]**, or `clear` within 30s.", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for("message", timeout=30.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True); return
        chall = self.cog.cfg(self.ctx.guild.id).setdefault("challenge", {})
        if msg.content.strip().lower() == "clear":
            chall["remove_role_id"] = None
        else:
            role = resolve_role_any(self.ctx.guild, msg.content)
            if not role:
                await interaction.followup.send("❌ Role not found.", ephemeral=True); return
            chall["remove_role_id"] = role.id
        await save_config(self.cog.bot.config)
        await interaction.followup.send("✅ Updated gated role to remove.", ephemeral=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Challenge: Replacement Role (grant)", style=discord.ButtonStyle.secondary)
    async def btn_challenge_grant(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Reply with **@Role**, **ID**, **[Exact Name]**, or `clear` within 30s.", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for("message", timeout=30.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
        except asyncio.TimeoutError:
            await interaction.followup.send.send_message("⏱️ Timed out.", ephemeral=True); return
        chall = self.cog.cfg(self.ctx.guild.id).setdefault("challenge", {})
        if msg.content.strip().lower() == "clear":
            chall["grant_role_id"] = None
        else:
            role = resolve_role_any(self.ctx.guild, msg.content)
            if not role:
                await interaction.followup.send("❌ Role not found.", ephemeral=True); return
            chall["grant_role_id"] = role.id
        await save_config(self.cog.bot.config)
        await interaction.followup.send("✅ Updated replacement role to grant.", ephemeral=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Set Welcome Text/Image", style=discord.ButtonStyle.success)
    async def btn_set_message(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Enter message as `Title | Description | optional_image_url` within 60s.", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for("message", timeout=60.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True); return
        parts = [p.strip() for p in msg.content.split("|")]
        title = parts[0] if parts else "Welcome!"
        desc = parts[1] if len(parts) > 1 else ""
        img = parts[2] if len(parts) > 2 else ""
        m = self.cog.cfg(self.ctx.guild.id).setdefault("message", {})
        m["title"], m["description"], m["image_url"] = title, desc, img
        await save_config(self.cog.bot.config)
        await interaction.followup.send("✅ Updated message.", ephemeral=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def btn_close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("👋 Closed.", ephemeral=True)
        await interaction.message.edit(view=None)
        self.stop()


class WelcomeGate(commands.Cog):
    """Welcome system: message, autorole, optional passphrase, and role swap on success."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active: Dict[int, Challenge] = {}  # user_id → Challenge
        self._used_codes: Set[str] = set()       # recently used to avoid reuse
        self._sweeper.start()

    def cog_unload(self):
        self._sweeper.cancel()

    def cfg(self, guild_id: int) -> Dict[str, Any]:
        all_cfg = self.bot.config.setdefault("welcome_gate", {})
        # guild-wide single section; if you need per-guild later, lift into dict keyed by guild_id
        # ensure defaults
        for k, v in DEFAULT_CFG.items():
            all_cfg.setdefault(k, v if not isinstance(v, dict) else v.copy())
        return all_cfg

    # ===== Code generation / lifecycle =====

    def _gen_code(self) -> str:
        # non-repeating among active + recently used
        for _ in range(1000):
            code = f"{random.randint(0, 999_999):06d}"
            if code not in self._used_codes and all(ch.code != code for ch in self._active.values()):
                return code
        # fallback (extremely unlikely to loop out)
        return f"{random.randint(0, 999_999):06d}"

    async def _log(self, guild: discord.Guild, text: str):
        cfg = self.cfg(guild.id)
        ch = resolve_channel_any(guild, cfg.get("log_channel_id"))
        if isinstance(ch, discord.TextChannel):
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())

    def _format_embed(self, guild: discord.Guild, member: discord.Member, code: Optional[str]) -> discord.Embed:
        cfg = self.cfg(guild.id)
        msg = cfg.get("message") or {}
        title = (msg.get("title") or "Welcome!")
        desc = (msg.get("description") or "").replace("{mention}", member.mention)
        if (cfg.get("challenge") or {}).get("enabled", True) and code:
            desc += f"\n\n**Passphrase:** `{code}`\nUse `!pass <code>` here or in DM within the time limit."
        emb = discord.Embed(title=title, description=desc, color=discord.Color.blurple(), timestamp=utcnow())
        if msg.get("image_url"):
            emb.set_image(url=msg["image_url"])
        return emb

    # ===== Background sweeper for expirations =====

    @tasks.loop(minutes=5)
    async def _sweeper(self):
        # prune expired challenges and shrink used code memory
        to_remove = []
        for uid, ch in list(self._active.items()):
            if ch.expired():
                to_remove.append(uid)
        for uid in to_remove:
            self._active.pop(uid, None)
        # cap used codes set to avoid unbounded growth
        if len(self._used_codes) > 20000:
            # drop arbitrary ~25% oldest by converting to list (good enough)
            for code in list(self._used_codes)[:5000]:
                self._used_codes.discard(code)

    @_sweeper.before_loop
    async def _before_sweeper(self):
        await self.bot.wait_until_ready()

    # ===== Events / Commands =====

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        cfg = self.cfg(guild.id)
        if not cfg.get("enabled", True):
            return

        # Autorole
        auto = resolve_role_any(guild, cfg.get("autorole_id"))
        if auto and can_manage_role(guild, auto):
            try:
                await member.add_roles(auto, reason="WelcomeGate autorole")
            except Exception:
                pass

        # Challenge
        chall = (cfg.get("challenge") or {})
        code: Optional[str] = None
        if chall.get("enabled", True):
            hours = clamp(int(chall.get("timeout_hours") or 72), 1, 72)
            code = self._gen_code()
            self._active[member.id] = Challenge(
                user_id=member.id,
                code=code,
                expires_at=utcnow() + timedelta(hours=hours),
            )

        # Message to channel and/or DM
        emb = self._format_embed(guild, member, code)
        if cfg.get("channel_id"):
            ch = resolve_channel_any(guild, cfg["channel_id"])
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send(member.mention, embed=emb, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                except Exception:
                    pass
        if cfg.get("dm_user"):
            try:
                await member.send(embed=emb)
            except Exception:
                pass

        await self._log(guild, f"👋 New member: {member.mention} (`{member.id}`){' with challenge' if code else ''}.")

    @commands.command(name="pass", aliases=["passphrase"])
    async def pass_cmd(self, ctx: commands.Context, code: str):
        # Allow in DM or in a guild channel
        member: Optional[discord.Member]
        if isinstance(ctx.channel, discord.DMChannel):
            # try to resolve user’s mutual guilds if needed; here we expect user to be in one guild
            if not ctx.author.mutual_guilds:
                await ctx.reply("❌ I can’t verify you here.")
                return
            guild = ctx.author.mutual_guilds[0]
            member = guild.get_member(ctx.author.id)
            if not member:
                try:
                    member = await guild.fetch_member(ctx.author.id)
                except Exception:
                    member = None
            if not member:
                await ctx.reply("❌ You’re not in the server.")
                return
        else:
            guild = ctx.guild  # type: ignore[assignment]
            member = ctx.author if isinstance(ctx.author, discord.Member) else None

        if not isinstance(guild, discord.Guild) or not member:
            await ctx.reply("❌ Not a valid context.")
            return

        cfg = self.cfg(guild.id)
        chall_cfg = cfg.get("challenge") or {}
        if not chall_cfg.get("enabled", True):
            await ctx.reply("ℹ️ No passphrase is required.")
            return

        ch = self._active.get(member.id)
        if not ch:
            await ctx.reply("❌ You don’t have an active challenge. Please ask staff.")
            return

        if ch.expired():
            self._active.pop(member.id, None)
            await ctx.reply("⏱️ Your challenge expired. Please ask staff.")
            await self._log(guild, f"⏱️ Challenge expired for {member.mention}.")
            return

        max_attempts = clamp(int(chall_cfg.get("max_attempts") or 5), 1, 10)
        if ch.attempts >= max_attempts:
            self._active.pop(member.id, None)
            await ctx.reply("❌ Maximum attempts exceeded. Please ask staff.")
            await self._log(guild, f"⛔ Max attempts exceeded for {member.mention}.")
            return

        if code.strip() == ch.code:
            # Success → swap roles
            gated = resolve_role_any(guild, chall_cfg.get("remove_role_id"))
            grant = resolve_role_any(guild, chall_cfg.get("grant_role_id"))
            # remove gated
            if gated and can_manage_role(guild, gated) and gated in member.roles:
                try:
                    await member.remove_roles(gated, reason="WelcomeGate verified - remove gated")
                except Exception:
                    pass
            # grant replacement
            if grant and can_manage_role(guild, grant) and grant not in member.roles:
                try:
                    await member.add_roles(grant, reason="WelcomeGate verified - grant")
                except Exception:
                    pass

            self._used_codes.add(ch.code)
            self._active.pop(member.id, None)
            await ctx.reply("✅ Verified. Welcome!")
            await self._log(guild, f"✅ {member.mention} verified successfully.")
        else:
            ch.attempts += 1
            remain = max_attempts - ch.attempts
            if remain <= 0:
                self._active.pop(member.id, None)
                await ctx.reply("❌ Incorrect. No attempts remaining. Please ask staff.")
                await self._log(guild, f"❌ Incorrect code; maxed attempts for {member.mention}.")
            else:
                await ctx.reply(f"❌ Incorrect. Attempts left: **{remain}**.")

    # ===== Admin surface =====

    @commands.has_permissions(administrator=True)
    @commands.command(name="welcomepanel")
    async def welcomepanel_cmd(self, ctx: commands.Context):
        view = WelcomeConsoleView(self, ctx)
        await ctx.send(embed=discord.Embed(title="Welcome Gate — Configuration", description="Loading…", color=discord.Color.blurple()), view=view)
        # fake interaction to render initial state
        class _Fake:
            message: discord.Message
            def __init__(self, msg): self.message = msg
        last = await ctx.channel.history(limit=1).flatten()
        if last:
            await view._refresh(_Fake(last[0]))  # update the embed with current cfg


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeGate(bot))
