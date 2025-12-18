# Feral_Kitty_FiFi/features/scheduler.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, time, date
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands, tasks

from ..config import save_config
from ..utils.discord_resolvers import resolve_channel_any, resolve_role_any

try:
    # Python 3.9+: stdlib
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# ---- Time helpers ----
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def parse_hhmm(val: str) -> Optional[time]:
    try:
        hh, mm = val.strip().split(":")
        return time(hour=int(hh), minute=int(mm))
    except Exception:
        return None

def _ensure_tz(dt_naive: datetime, tz: Optional[str]) -> datetime:
    """Attach local tz then convert to UTC."""
    if tz and ZoneInfo:
        try:
            z = ZoneInfo(tz)
            # treat input as local time in that tz
            dt_local = dt_naive.replace(tzinfo=z)
            return dt_local.astimezone(timezone.utc)
        except Exception:
            pass
    # fallback assume input is already UTC
    return dt_naive.replace(tzinfo=timezone.utc)

def to_utc_from_local(date_part: date, t: time, tz: Optional[str]) -> datetime:
    naive = datetime.combine(date_part, t)
    return _ensure_tz(naive, tz)

def next_daily_local(t: time, tz: Optional[str], now_utc: Optional[datetime] = None) -> datetime:
    now_utc = now_utc or utcnow()
    if tz and ZoneInfo:
        try:
            z = ZoneInfo(tz)
            now_local = now_utc.astimezone(z)
            today_local = datetime.combine(now_local.date(), t, tzinfo=z)
            target_local = today_local if today_local > now_local else today_local + timedelta(days=1)
            return target_local.astimezone(timezone.utc)
        except Exception:
            pass
    # no tz support → assume UTC clock
    today_utc = datetime.combine((now_utc.date()), t, tzinfo=timezone.utc)
    return today_utc if today_utc > now_utc else today_utc + timedelta(days=1)

def next_weekly_local(days: List[int], t: time, tz: Optional[str], now_utc: Optional[datetime] = None) -> datetime:
    now_utc = now_utc or utcnow()
    days = sorted({d for d in days if 0 <= d <= 6})  # Mon=0..Sun=6
    if not days:
        return next_daily_local(t, tz, now_utc)
    if tz and ZoneInfo:
        try:
            z = ZoneInfo(tz)
            now_local = now_utc.astimezone(z)
            for i in range(0, 8):
                cand_date = now_local.date() + timedelta(days=i)
                if cand_date.weekday() in days:
                    cand_local = datetime.combine(cand_date, t, tzinfo=z)
                    if cand_local > now_local:
                        return cand_local.astimezone(timezone.utc)
        except Exception:
            pass
    # Fallback in UTC
    nowu = now_utc
    for i in range(0, 8):
        cand = datetime.combine(nowu.date() + timedelta(days=i), t, tzinfo=timezone.utc)
        if cand.weekday() in days and cand > nowu:
            return cand
    return nowu + timedelta(days=1)


# ---- Config backbone ----
def _sched_cfg(bot: commands.Bot) -> Dict[str, Any]:
    root = bot.config.setdefault("scheduler", {})
    root.setdefault("jobs", [])
    return root

def _next_job_id(jobs: List[Dict[str, Any]]) -> int:
    return (max([int(j.get("id", 0)) for j in jobs], default=0) + 1) or 1


# ---- Recurrence engine ----
def compute_next_run(job: Dict[str, Any], now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or utcnow()
    rec = (job.get("recurrence") or {})
    rtype = (rec.get("type") or "once").lower()
    tz = (job.get("tz") or "").strip() or None

    if rtype == "once":
        at_iso = rec.get("at_iso")
        # at_iso may be in local wall-clock if tz set; try both ISO and "YYYY-MM-DD HH:MM"
        dt = None
        if at_iso:
            # try full iso first
            try:
                maybe = datetime.fromisoformat(at_iso.replace("Z", "+00:00"))
                if maybe.tzinfo:
                    dt = maybe.astimezone(timezone.utc)
                else:
                    dt = _ensure_tz(maybe, tz)
            except Exception:
                dt = None
        if not dt and isinstance(at_iso, str):
            try:
                maybe = datetime.strptime(at_iso, "%Y-%m-%d %H:%M")
                dt = _ensure_tz(maybe, tz)
            except Exception:
                dt = None
        return dt

    if rtype == "interval":
        minutes = int(rec.get("minutes") or 0)
        minutes = max(1, min(60 * 24 * 7, minutes))  # cap 1w
        last = None
        try:
            last = datetime.fromisoformat((job.get("last_run_iso") or "").replace("Z", "+00:00"))
        except Exception:
            last = None
        base = last or now
        nxt = base + timedelta(minutes=minutes)
        if nxt <= now:
            nxt = now + timedelta(seconds=5)
        return nxt

    if rtype == "daily":
        t = parse_hhmm(str(rec.get("time") or "00:00")) or time(0, 0)
        return next_daily_local(t, tz, now)

    if rtype == "weekly":
        t = parse_hhmm(str(rec.get("time") or "00:00")) or time(0, 0)
        days = rec.get("days") or []
        return next_weekly_local(days, t, tz, now)

    return None


# ---- Parsing helpers ----
def _split_tokens(raw: str) -> List[str]:
    return [t.strip() for t in (raw or "").replace(",", " ").split() if t.strip()]

def parse_channels(guild: discord.Guild, raw: str) -> List[int]:
    ids: List[int] = []
    for tok in _split_tokens(raw):
        ch = resolve_channel_any(guild, tok)
        if isinstance(ch, discord.TextChannel):
            ids.append(ch.id)
    out, seen = [], set()
    for cid in ids:
        if cid not in seen:
            out.append(cid); seen.add(cid)
    return out

def parse_roles(guild: discord.Guild, raw: str) -> List[int]:
    ids: List[int] = []
    for tok in _split_tokens(raw):
        r = resolve_role_any(guild, tok)
        if r:
            ids.append(r.id)
    out, seen = [], set()
    for rid in ids:
        if rid not in seen:
            out.append(rid); seen.add(rid)
    return out

def parse_urls(raw: str) -> List[str]:
    urls: List[str] = []
    for tok in _split_tokens(raw):
        if tok.startswith("http://") or tok.startswith("https://"):
            urls.append(tok)
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            out.append(u); seen.add(u)
    return out

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---- Console UI ----
class SchedulerConsoleView(discord.ui.View):
    """Admin console for creating, previewing, editing and managing scheduled messages."""

    def __init__(self, cog: "Scheduler", ctx: commands.Context):
        super().__init__(timeout=600)
        self.cog = cog
        self.ctx = ctx

    async def _refresh(self, interaction: discord.Interaction):
        jobs = list(_sched_cfg(self.cog.bot)["jobs"])
        lines: List[str] = []
        for j in jobs[:15]:
            status = "🟢" if j.get("active") else "⏸️"
            r = j.get("recurrence", {})
            rtype = (r.get("type") or "once").lower()
            nxt = j.get("next_run_iso") or "n/a"
            tz = j.get("tz") or "UTC"
            lines.append(f"{status} `#{j.get('id')}` **{j.get('name','(no name)')}** · `{rtype}` · next: {nxt} · tz: {tz}")
        desc = "\n".join(lines) if lines else "_No jobs configured._"

        emb = discord.Embed(
            title="Scheduler — Jobs",
            description=desc,
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        emb.set_footer(text="Use buttons to add/list/preview/edit/pause/resume/delete.")
        await interaction.message.edit(embed=emb, view=self)

    # ---------- Create ----------
    @discord.ui.button(label="New Job", style=discord.ButtonStyle.success)
    async def btn_new(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "Reply with the following **within 90s**, each on a new message:\n"
            "1) **Name**\n"
            "2) **Channels** (mentions/IDs/names, space/comma)\n"
            "3) **Roles to ping** (`none` to skip)\n"
            "4) **Embed Title**\n"
            "5) **Embed Description**\n"
            "6) **Image URL** (`none` to skip)\n"
            "7) **Attachments URLs** (space/comma; `none` to skip)\n"
            "8) **Recurrence**:\n"
            "   - `once 2025-12-31 23:59`\n"
            "   - `interval 60`\n"
            "   - `daily 09:30`\n"
            "   - `weekly 1,3,5 20:00`\n"
            "9) **Timezone** (IANA, e.g. `America/New_York`; `UTC` or `none` for UTC)\n"
            "10) **Active?** (`yes`/`no`)\n",
            ephemeral=True
        )

        def _ck(m: discord.Message) -> bool:
            return m.author == interaction.user and m.channel == self.ctx.channel

        try:
            name = (await self.cog.bot.wait_for("message", timeout=90, check=_ck)).content.strip()
            chs = (await self.cog.bot.wait_for("message", timeout=90, check=_ck)).content.strip()
            ros = (await self.cog.bot.wait_for("message", timeout=90, check=_ck)).content.strip()
            et = (await self.cog.bot.wait_for("message", timeout=90, check=_ck)).content.strip()
            ed = (await self.cog.bot.wait_for("message", timeout=90, check=_ck)).content.strip()
            img = (await self.cog.bot.wait_for("message", timeout=90, check=_ck)).content.strip()
            atc = (await self.cog.bot.wait_for("message", timeout=90, check=_ck)).content.strip()
            rec = (await self.cog.bot.wait_for("message", timeout=90, check=_ck)).content.strip()
            tz = (await self.cog.bot.wait_for("message", timeout=90, check=_ck)).content.strip()
            act = (await self.cog.bot.wait_for("message", timeout=90, check=_ck)).content.strip().lower()
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out creating job.", ephemeral=True)
            return

        guild = self.ctx.guild
        channel_ids = parse_channels(guild, chs)
        role_ids = [] if ros.lower() == "none" else parse_roles(guild, ros)
        image_url = "" if img.lower() == "none" else img
        attachments = [] if atc.lower() == "none" else parse_urls(atc)

        # parse recurrence
        parts = rec.split()
        rtype = (parts[0] if parts else "once").lower()
        rdata: Dict[str, Any] = {"type": rtype}
        if rtype == "once" and len(parts) >= 3:
            at_str = f"{parts[1]} {parts[2]}".strip()
            rdata["at_iso"] = at_str  # interpret later with tz
        elif rtype == "interval" and len(parts) >= 2:
            try:
                minutes = max(1, min(60 * 24 * 7, int(parts[1])))
            except Exception:
                minutes = 60
            rdata["minutes"] = minutes
        elif rtype == "daily" and len(parts) >= 2:
            rdata["time"] = parts[1]
        elif rtype == "weekly" and len(parts) >= 3:
            days = [int(x) for x in parts[1].replace(",", " ").split() if x.isdigit()]
            rdata["days"] = days
            rdata["time"] = parts[2]
        else:
            rdata = {"type": "interval", "minutes": 60}

        tz = None if tz.lower() in ("none", "utc", "") else tz

        job = {
            "id": _next_job_id(_sched_cfg(self.cog.bot)["jobs"]),
            "name": name or "Scheduled Message",
            "active": act in ("yes", "y", "true", "1"),
            "channels": channel_ids,
            "role_ids": role_ids,
            "embed": {
                "title": et or "Announcement",
                "description": ed or "",
                "image_url": image_url,
                "footer": ""
            },
            "attachments": attachments,
            "recurrence": rdata,
            "tz": tz or "UTC",
            "last_run_iso": None,
            "next_run_iso": None
        }
        nxt = compute_next_run(job, utcnow())
        job["next_run_iso"] = nxt.isoformat() if nxt else None

        cfg = _sched_cfg(self.cog.bot)
        cfg["jobs"].append(job)
        await save_config(self.cog.bot.config)

        await interaction.followup.send(f"✅ Created job `#{job['id']}`. Next run: `{job['next_run_iso']}`", ephemeral=True)
        await self._refresh(interaction)

    # ---------- List ----------
    @discord.ui.button(label="List Jobs", style=discord.ButtonStyle.secondary)
    async def btn_list(self, interaction: discord.Interaction, _: discord.ui.Button):
        jobs = list(_sched_cfg(self.cog.bot)["jobs"])
        if not jobs:
            await interaction.response.send_message("ℹ️ No jobs.", ephemeral=True); return
        lines = []
        for j in jobs:
            lines.append(f"`#{j['id']}` • **{j.get('name','(no name)')}** • active={j.get('active')} • next={j.get('next_run_iso')} • tz={j.get('tz','UTC')}")
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    # ---------- Preview ----------
    @discord.ui.button(label="Preview by ID", style=discord.ButtonStyle.secondary)
    async def btn_preview(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Enter job **ID** to preview here within 30s.", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for("message", timeout=30.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True); return
        try:
            jid = int(msg.content.strip())
        except Exception:
            await interaction.followup.send("❌ Invalid ID.", ephemeral=True); return

        job = None
        for j in _sched_cfg(self.cog.bot)["jobs"]:
            if int(j.get("id")) == jid:
                job = j; break
        if not job:
            await interaction.followup.send("❌ Job not found.", ephemeral=True); return

        # Build preview
        embed_data = job.get("embed") or {}
        emb = discord.Embed(
            title=f"[PREVIEW] {embed_data.get('title') or 'Announcement'}",
            description=embed_data.get("description") or "",
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        if embed_data.get("image_url"):
            emb.set_image(url=embed_data["image_url"])
        if embed_data.get("footer"):
            emb.set_footer(text=str(embed_data["footer"]))

        role_mentions = []
        for rid in job.get("role_ids") or []:
            r = self.ctx.guild.get_role(int(rid))
            if r:
                role_mentions.append(r.mention)
        content = " ".join(role_mentions) if role_mentions else None
        allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)
        await self.ctx.send(content=content, embed=emb, allowed_mentions=allowed)
        await interaction.followup.send("✅ Preview sent to this channel.", ephemeral=True)

    # ---------- Toggle ----------
    @discord.ui.button(label="Pause/Resume by ID", style=discord.ButtonStyle.secondary)
    async def btn_toggle(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Enter job **ID** to toggle active/pause within 30s.", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for("message", timeout=30.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True); return
        try:
            jid = int(msg.content.strip())
        except Exception:
            await interaction.followup.send("❌ Invalid ID.", ephemeral=True); return
        jobs = _sched_cfg(self.cog.bot)["jobs"]
        for j in jobs:
            if int(j.get("id")) == jid:
                j["active"] = not bool(j.get("active"))
                if j["active"] and not j.get("next_run_iso"):
                    nxt = compute_next_run(j, utcnow())
                    j["next_run_iso"] = nxt.isoformat() if nxt else None
                await save_config(self.cog.bot.config)
                await interaction.followup.send(f"✅ Job #{jid} active={j['active']}.", ephemeral=True)
                await self._refresh(interaction)
                return
        await interaction.followup.send("❌ Job not found.", ephemeral=True)

    # ---------- Delete ----------
    @discord.ui.button(label="Delete by ID", style=discord.ButtonStyle.danger)
    async def btn_delete(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Enter job **ID** to delete within 30s.", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for("message", timeout=30.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True); return
        try:
            jid = int(msg.content.strip())
        except Exception:
            await interaction.followup.send("❌ Invalid ID.", ephemeral=True); return
        jobs = _sched_cfg(self.cog.bot)["jobs"]
        for i, j in enumerate(jobs):
            if int(j.get("id")) == jid:
                jobs.pop(i)
                await save_config(self.cog.bot.config)
                await interaction.followup.send(f"🗑️ Deleted job #{jid}.", ephemeral=True)
                await self._refresh(interaction)
                return
        await interaction.followup.send("❌ Job not found.", ephemeral=True)

    # ---------- Edit ----------
    @discord.ui.button(label="Edit Job", style=discord.ButtonStyle.primary)
    async def btn_edit(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "Enter job **ID** to edit within 30s.\n"
            "Then I’ll ask what to change: `name`, `channels`, `roles`, `embed`, `attachments`, `recurrence`, `timezone`, `active`.",
            ephemeral=True,
        )
        try:
            msg = await self.cog.bot.wait_for("message", timeout=30.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True); return
        try:
            jid = int(msg.content.strip())
        except Exception:
            await interaction.followup.send("❌ Invalid ID.", ephemeral=True); return

        jobs = _sched_cfg(self.cog.bot)["jobs"]
        job = next((j for j in jobs if int(j.get("id")) == jid), None)
        if not job:
            await interaction.followup.send("❌ Job not found.", ephemeral=True); return

        await interaction.followup.send(
            "What field group to edit? (`name`/`channels`/`roles`/`embed`/`attachments`/`recurrence`/`timezone`/`active`)\n"
            "Reply within 45s.",
            ephemeral=True,
        )
        try:
            q = await self.cog.bot.wait_for("message", timeout=45.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True); return

        field = q.content.strip().lower()
        try:
            if field == "name":
                await interaction.followup.send("Enter new name:", ephemeral=True)
                nm = await self.cog.bot.wait_for("message", timeout=45.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
                job["name"] = nm.content.strip() or job.get("name")
            elif field == "channels":
                await interaction.followup.send("Enter channels (mentions/IDs/names, space/comma):", ephemeral=True)
                chs = await self.cog.bot.wait_for("message", timeout=45.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
                job["channels"] = parse_channels(self.ctx.guild, chs.content)
            elif field == "roles":
                await interaction.followup.send("Enter roles to ping (`none` for none):", ephemeral=True)
                rs = await self.cog.bot.wait_for("message", timeout=45.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
                job["role_ids"] = [] if rs.content.strip().lower() == "none" else parse_roles(self.ctx.guild, rs.content)
            elif field == "embed":
                await interaction.followup.send("Enter as `Title | Description | optional_image_url | optional_footer`:", ephemeral=True)
                em = await self.cog.bot.wait_for("message", timeout=60.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
                parts = [p.strip() for p in em.content.split("|")]
                ed = job.setdefault("embed", {})
                ed["title"] = parts[0] if parts else ed.get("title") or "Announcement"
                ed["description"] = parts[1] if len(parts) > 1 else ed.get("description") or ""
                ed["image_url"] = parts[2] if len(parts) > 2 else ed.get("image_url") or ""
                ed["footer"] = parts[3] if len(parts) > 3 else ed.get("footer") or ""
            elif field == "attachments":
                await interaction.followup.send("Enter attachment URLs (space/comma; `none` for none):", ephemeral=True)
                atc = await self.cog.bot.wait_for("message", timeout=45.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
                job["attachments"] = [] if atc.content.strip().lower() == "none" else parse_urls(atc.content)
            elif field == "recurrence":
                await interaction.followup.send(
                    "Enter recurrence:\n"
                    "- `once 2025-12-31 23:59`\n"
                    "- `interval 60`\n"
                    "- `daily 09:30`\n"
                    "- `weekly 1,3,5 20:00`",
                    ephemeral=True,
                )
                rr = await self.cog.bot.wait_for("message", timeout=60.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
                parts = rr.content.split()
                rtype = (parts[0] if parts else "once").lower()
                rdata: Dict[str, Any] = {"type": rtype}
                if rtype == "once" and len(parts) >= 3:
                    rdata["at_iso"] = f"{parts[1]} {parts[2]}"
                elif rtype == "interval" and len(parts) >= 2:
                    try:
                        rdata["minutes"] = max(1, min(60 * 24 * 7, int(parts[1])))
                    except Exception:
                        rdata["minutes"] = 60
                elif rtype == "daily" and len(parts) >= 2:
                    rdata["time"] = parts[1]
                elif rtype == "weekly" and len(parts) >= 3:
                    days = [int(x) for x in parts[1].replace(",", " ").split() if x.isdigit()]
                    rdata["days"] = days
                    rdata["time"] = parts[2]
                job["recurrence"] = rdata
                # recompute next
                job["next_run_iso"] = (compute_next_run(job) or utcnow()).isoformat()
            elif field == "timezone":
                await interaction.followup.send("Enter IANA timezone (e.g. `America/New_York`) or `UTC`:", ephemeral=True)
                tz = await self.cog.bot.wait_for("message", timeout=45.0, check=lambda m: m.author == interaction.user and m.channel == self.ctx.channel)
                val = tz.content.strip()
                job["tz"] = None if val.lower() in ("utc", "none", "") else val
                job["next_run_iso"] = (compute_next_run(job) or utcnow()).isoformat()
            elif field == "active":
                job["active"] = not bool(job.get("active"))
                if job["active"] and not job.get("next_run_iso"):
                    nxt = compute_next_run(job, utcnow())
                    job["next_run_iso"] = nxt.isoformat() if nxt else None
            else:
                await interaction.followup.send("❌ Unknown field group.", ephemeral=True); return

            await save_config(self.cog.bot.config)
            await interaction.followup.send(f"✅ Updated job #{jid}.", ephemeral=True)
            # refresh summary
            await self._refresh(interaction)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏱️ Timed out.", ephemeral=True)

    # ---------- Close ----------
    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def btn_close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("👋 Closed.", ephemeral=True)
        await interaction.message.edit(view=None)
        self.stop()


# ---- Cog with dispatcher loop ----
class Scheduler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._tick.start()

    def cog_unload(self):
        self._tick.cancel()

    @commands.has_permissions(administrator=True)
    @commands.command(name="schedulepanel")
    async def schedulepanel_cmd(self, ctx: commands.Context):
        view = SchedulerConsoleView(self, ctx)
        msg = await ctx.send(embed=discord.Embed(title="Scheduler — Jobs", description="Loading…", color=discord.Color.blurple(), timestamp=utcnow()), view=view)
        class _F:  # minimal shim to reuse _refresh
            def __init__(self, m): self.message = m
        await view._refresh(_F(msg))

    @tasks.loop(seconds=30)
    async def _tick(self):
        await self._dispatch_due_jobs()

    @_tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    async def _dispatch_due_jobs(self):
        cfg = _sched_cfg(self.bot)
        jobs: List[Dict[str, Any]] = cfg["jobs"]
        changed = False
        now = utcnow()

        for job in jobs:
            if not job.get("active"):
                continue
            nxt_iso = job.get("next_run_iso")
            try:
                nxt = datetime.fromisoformat(nxt_iso.replace("Z", "+00:00")) if isinstance(nxt_iso, str) else None
            except Exception:
                nxt = None
            if not nxt or nxt > now:
                continue

            # Build message
            embed_data = job.get("embed") or {}
            emb = discord.Embed(
                title=embed_data.get("title") or "Announcement",
                description=embed_data.get("description") or "",
                color=discord.Color.blurple(),
                timestamp=utcnow(),
            )
            if embed_data.get("image_url"):
                emb.set_image(url=embed_data["image_url"])
            if embed_data.get("footer"):
                emb.set_footer(text=str(embed_data["footer"]))

            # Mentions
            role_mentions = []
            for rid in job.get("role_ids") or []:
                for g in self.bot.guilds:
                    r = g.get_role(int(rid))
                    if r:
                        role_mentions.append(r.mention); break
            content = " ".join(role_mentions) if role_mentions else None
            allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)

            # Send to channels
            for cid in job.get("channels") or []:
                ch = None
                for g in self.bot.guilds:
                    ch = g.get_channel(int(cid)) or ch
                if isinstance(ch, discord.TextChannel):
                    try:
                        urls = job.get("attachments") or []
                        if urls:
                            emb2 = emb.copy()
                            extra = "\n".join(urls)
                            if emb2.description:
                                emb2.description += f"\n\n{extra}"
                            else:
                                emb2.description = extra
                            await ch.send(content=content, embed=emb2, allowed_mentions=allowed)
                        else:
                            await ch.send(content=content, embed=emb, allowed_mentions=allowed)
                    except Exception:
                        pass

            # Update schedule
            job["last_run_iso"] = now.isoformat()
            nxt2 = compute_next_run(job, now)
            if (job.get("recurrence") or {}).get("type") == "once":
                job["active"] = False
                job["next_run_iso"] = None
            else:
                job["next_run_iso"] = nxt2.isoformat() if nxt2 else None
            changed = True

        if changed:
            await save_config(self.bot.config)


async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduler(bot))
