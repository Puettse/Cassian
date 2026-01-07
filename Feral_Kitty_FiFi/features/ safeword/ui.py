from __future__ import annotations
from typing import List, Any
import discord

from .config import ensure_sw_cfg
from .constants import STAFF_FALLBACK_NAME

class SafewordConfigModal(discord.ui.Modal, title="Safeword: Text & Timing"):
    def __init__(self, *, bot: discord.Client, seed: dict):
        super().__init__(timeout=300)
        self.bot = bot
        self.seed = seed  # selections from the View

        # Text inputs (defaults filled from current config if present)
        self.trigger = discord.ui.TextInput(
            label="Trigger (exact match)",
            style=discord.TextStyle.short,
            default=seed.get("trigger") or "!STOP!",
            max_length=64,
            required=True,
        )
        self.release_trigger = discord.ui.TextInput(
            label="Release Trigger (exact match)",
            style=discord.TextStyle.short,
            default=seed.get("release_trigger") or "!Release",
            max_length=64,
            required=True,
        )
        self.cooldown = discord.ui.TextInput(
            label="Cooldown seconds (0 disables)",
            style=discord.TextStyle.short,
            default=str(seed.get("cooldown_seconds") or 30),
            max_length=6,
            required=True,
        )
        self.history_limit = discord.ui.TextInput(
            label="Transcript history limit (1-100)",
            style=discord.TextStyle.short,
            default=str(seed.get("history_limit") or 25),
            max_length=3,
            required=True,
        )
        self.lock_message = discord.ui.TextInput(
            label="Lock message text",
            style=discord.TextStyle.paragraph,
            default=str((seed.get("lock_message") or {}).get("text") or "🚨 Safeword triggered. Channel is temporarily locked. A responder will assist shortly."),
            required=False,
            max_length=1000,
        )
        self.release_message = discord.ui.TextInput(
            label="Release message text",
            style=discord.TextStyle.paragraph,
            default=str((seed.get("release_message") or {}).get("text") or "✅ Channel has been released. Please continue respectfully."),
            required=False,
            max_length=1000,
        )

        self.add_item(self.trigger)
        self.add_item(self.release_trigger)
        self.add_item(self.cooldown)
        self.add_item(self.history_limit)
        self.add_item(self.lock_message)
        self.add_item(self.release_message)

    async def on_submit(self, interaction: discord.Interaction):
        # Write config to bot.config["safeword"]
        cfg = ensure_sw_cfg(self.bot)

        # Role/channel selections came from View (seed)
        cfg["roles_to_ping"] = self.seed.get("roles_to_ping", [])
        cfg["roles_whitelist"] = self.seed.get("roles_whitelist", [STAFF_FALLBACK_NAME])
        cfg["blocked_roles"] = self.seed.get("blocked_roles", [])
        if "log_channel_id" in self.seed:
            cfg["log_channel_id"] = self.seed["log_channel_id"]

        # Modal fields
        cfg["enabled"] = True
        cfg["trigger"] = str(self.trigger.value).strip()
        cfg["release_trigger"] = str(self.release_trigger.value).strip()
        cfg["cooldown_seconds"] = max(0, int(str(self.cooldown.value).strip() or "0"))
        hl = max(1, min(100, int(str(self.history_limit.value).strip() or "25")))
        cfg["history_limit"] = hl
        cfg["lock_message"] = {"text": str(self.lock_message.value or "").strip(), "image_url": (cfg.get("lock_message") or {}).get("image_url") or ""}
        cfg["release_message"] = {"text": str(self.release_message.value or "").strip(), "image_url": (cfg.get("release_message") or {}).get("image_url") or ""}

        # Private confirmation
        roles_to_ping_txt = ", ".join([f"<@&{rid}>" if isinstance(rid, int) else f"`{rid}`" for rid in cfg["roles_to_ping"]]) or "—"
        roles_whitelist_txt = ", ".join([f"<@&{rid}>" if isinstance(rid, int) else f"`{rid}`" for rid in cfg["roles_whitelist"]]) or "—"
        blocked_roles_txt = ", ".join([f"<@&{rid}>" if isinstance(rid, int) else f"`{rid}`" for rid in cfg["blocked_roles"]]) or "—"
        log_chan_txt = f"<#{cfg['log_channel_id']}>" if isinstance(cfg.get("log_channel_id"), int) else "—"

        await interaction.response.send_message(
            ephemeral=True,
            content=(
                "✅ **Safeword configured**\n"
                f"• Trigger: `{cfg['trigger']}`   • Release: `{cfg['release_trigger']}`\n"
                f"• Cooldown: `{cfg['cooldown_seconds']}s`   • History: `{cfg['history_limit']}`\n"
                f"• Roles to ping: {roles_to_ping_txt}\n"
                f"• Whitelist: {roles_whitelist_txt}\n"
                f"• Blocked: {blocked_roles_txt}\n"
                f"• Log channel: {log_chan_txt}\n"
            ),
        )

class SafewordConfigView(discord.ui.View):
    """Ephemeral view used by /safeword_setup."""
    def __init__(self, *, bot: discord.Client, seed: dict):
        super().__init__(timeout=600)
        self.bot = bot
        self.seed = seed  # start with existing cfg

        # --- Role / channel selects ---
        self.roles_to_ping = discord.ui.RoleSelect(
            placeholder="Select Roles to Ping (0–5)",
            min_values=0, max_values=5,
        )
        self.roles_whitelist = discord.ui.RoleSelect(
            placeholder="Select Whitelist Roles (0–5)",
            min_values=0, max_values=5,
        )
        self.blocked_roles = discord.ui.RoleSelect(
            placeholder="Select Blocked Roles (optional, 0–5)",
            min_values=0, max_values=5,
        )
        self.log_channel = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            placeholder="Select #SAFEWORD log channel",
        )

        self.add_item(self.roles_to_ping)
        self.add_item(self.roles_whitelist)
        self.add_item(self.blocked_roles)
        self.add_item(self.log_channel)

    @discord.ui.button(label="Next: Messages & Timing", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # collect selections from the view
        def ids_from_roles(sel: discord.ui.RoleSelect) -> List[Any]:
            # discord.py provides .values as List[discord.Role]
            return [r.id for r in sel.values] if getattr(sel, "values", None) else []

        roles_to_ping_ids = ids_from_roles(self.roles_to_ping)
        roles_whitelist_ids = ids_from_roles(self.roles_whitelist)
        blocked_roles_ids = ids_from_roles(self.blocked_roles)
        log_channel_ids = [c.id for c in self.log_channel.values] if getattr(self.log_channel, "values", None) else []

        # seed into modal
        seed = dict(self.seed)
        seed["roles_to_ping"] = roles_to_ping_ids
        seed["roles_whitelist"] = roles_whitelist_ids or [STAFF_FALLBACK_NAME]
        seed["blocked_roles"] = blocked_roles_ids
        if log_channel_ids:
            seed["log_channel_id"] = log_channel_ids[0]

        modal = SafewordConfigModal(bot=self.bot, seed=seed)
        await interaction.response.send_modal(modal)
