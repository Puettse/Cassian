from __future__ import annotations
from typing import List, Any, Dict, Optional
import discord

from .config import ensure_sw_cfg, sw_cfg
from .constants import STAFF_FALLBACK_NAME

def _id_list(values, attr="id"):
    try:
        return [getattr(v, attr) for v in values] if values else []
    except Exception:
        return []

class SafewordConfigModal(discord.ui.Modal, title="Safeword: Messages & Timing"):
    def __init__(self, *, bot: discord.Client, seed: dict, author_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.seed = seed
        self.author_id = author_id

        self.trigger = discord.ui.TextInput(
            label="Trigger (exact match)",
            style=discord.TextStyle.short,
            default=seed.get("trigger") or "!STOP!",
            required=True, max_length=64,
        )
        self.release_trigger = discord.ui.TextInput(
            label="Release Trigger (exact match)",
            style=discord.TextStyle.short,
            default=seed.get("release_trigger") or "!Release",
            required=True, max_length=64,
        )
        self.cooldown = discord.ui.TextInput(
            label="Cooldown seconds (0 disables)",
            style=discord.TextStyle.short,
            default=str(seed.get("cooldown_seconds") or 30),
            required=True, max_length=6,
        )
        self.history_limit = discord.ui.TextInput(
            label="Transcript history limit (1-100)",
            style=discord.TextStyle.short,
            default=str(seed.get("history_limit") or 25),
            required=True, max_length=3,
        )
        self.lock_message = discord.ui.TextInput(
            label="Lock message text",
            style=discord.TextStyle.paragraph,
            default=str((seed.get("lock_message") or {}).get("text") or
                        "🚨 Safeword triggered. Channel is temporarily locked. A responder will assist shortly."),
            required=False, max_length=1000,
        )
        self.release_message = discord.ui.TextInput(
            label="Release message text",
            style=discord.TextStyle.paragraph,
            default=str((seed.get("release_message") or {}).get("text") or
                        "✅ Channel has been released. Please continue respectfully."),
            required=False, max_length=1000,
        )

        self.add_item(self.trigger)
        self.add_item(self.release_trigger)
        self.add_item(self.cooldown)
        self.add_item(self.history_limit)
        self.add_item(self.lock_message)
        self.add_item(self.release_message)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("❌ This setup isn’t yours.", ephemeral=True)

        cfg = ensure_sw_cfg(self.bot)

        # Persist role/channel picks from seed (set by the View)
        cfg["roles_to_ping"] = self.seed.get("roles_to_ping", [])
        cfg["roles_whitelist"] = self.seed.get("roles_whitelist", [STAFF_FALLBACK_NAME])
        cfg["blocked_roles"] = self.seed.get("blocked_roles", [])
        if "log_channel_id" in self.seed:
            cfg["log_channel_id"] = self.seed["log_channel_id"]

        # Persist modal fields
        cfg["enabled"] = True
        cfg["trigger"] = str(self.trigger.value).strip()
        cfg["release_trigger"] = str(self.release_trigger.value).strip()
        cfg["cooldown_seconds"] = max(0, int(str(self.cooldown.value or "0")))
        cfg["history_limit"] = max(1, min(100, int(str(self.history_limit.value or "25"))))
        cfg["lock_message"] = {
            "text": str(self.lock_message.value or "").strip(),
            "image_url": (cfg.get("lock_message") or {}).get("image_url") or "",
        }
        cfg["release_message"] = {
            "text": str(self.release_message.value or "").strip(),
            "image_url": (cfg.get("release_message") or {}).get("image_url") or "",
        }

        # Confirmation (ephemeral)
        def fmt(items):
            return ", ".join([f"<@&{i}>" if isinstance(i, int) else f"`{i}`" for i in (items or [])]) or "—"
        roles_to_ping_txt = fmt(cfg.get("roles_to_ping"))
        roles_whitelist_txt = fmt(cfg.get("roles_whitelist"))
        blocked_roles_txt = fmt(cfg.get("blocked_roles"))
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
    """Dropdowns for roles/channels; only the invoker can use it."""
    def __init__(self, *, bot: discord.Client, author_id: int, seed: Optional[Dict]=None):
        super().__init__(timeout=600)
        self.bot = bot
        self.author_id = author_id
        self.seed = dict(seed or {})

        # Live dropdowns (guild-only)
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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only allow the original author to use this view."""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("🚫 This setup is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Next: Messages & Timing", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Collect current selections
        roles_to_ping_ids = _id_list(getattr(self.roles_to_ping, "values", []))
        roles_whitelist_ids = _id_list(getattr(self.roles_whitelist, "values", []))
        blocked_roles_ids = _id_list(getattr(self.blocked_roles, "values", []))
        log_channel_ids = _id_list(getattr(self.log_channel, "values", []))

        # Seed defaults from existing config if unset
        seed = dict(sw_cfg(self.bot))
        seed["roles_to_ping"] = roles_to_ping_ids or seed.get("roles_to_ping", [])
        seed["roles_whitelist"] = roles_whitelist_ids or seed.get("roles_whitelist", [STAFF_FALLBACK_NAME])
        seed["blocked_roles"] = blocked_roles_ids or seed.get("blocked_roles", [])
        if log_channel_ids:
            seed["log_channel_id"] = log_channel_ids[0]

        modal = SafewordConfigModal(bot=self.bot, seed=seed, author_id=self.author_id)
        await interaction.response.send_modal(modal)
