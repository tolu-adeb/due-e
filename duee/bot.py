"""Discord side of DUE-E: scheduled posts + slash commands. Also hosts the local web app."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from . import render
from .analysis import _fmt_h
from .app import DueE
from .scheduler import Scheduler

log = logging.getLogger(__name__)
NO_PINGS = discord.AllowedMentions.none()


class ChannelPoster:
    def __init__(self, client: discord.Client, channel_id: int):
        self.client, self.channel_id = client, channel_id
        self._channel: discord.abc.Messageable | None = None

    async def post(self, text: str) -> None:
        if self._channel is None:
            self._channel = self.client.get_channel(self.channel_id) or await self.client.fetch_channel(self.channel_id)
        await self._channel.send(text, allowed_mentions=NO_PINGS, suppress_embeds=True)


def _parse_minutes(text: str) -> int:
    """'45', '45m', '1h', '1h30', '1.5h' → minutes."""
    import re
    m = re.fullmatch(r"\s*(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+)\s*m?)?\s*", text.lower())
    if not m or not (m.group(1) or m.group(2)):
        raise ValueError("use a duration like 45m, 1h or 1h30")
    return round(float(m.group(1) or 0) * 60 + int(m.group(2) or 0))


class DueeBot(discord.Client):
    def __init__(self, due: DueE):
        super().__init__(intents=discord.Intents.default())  # no privileged intents needed
        self.due = due
        self.cfg, self.radar, self.health = due.cfg, due.radar, due.status
        self.tree = app_commands.CommandTree(self)
        self.scheduler = Scheduler(due.cfg, due.radar, ChannelPoster(self, due.cfg.channel_id), due.status, due=due)
        self._web = None
        self._register_commands()

    async def setup_hook(self) -> None:
        if self.cfg.guild_id:  # guild sync is instant; global sync can take up to an hour
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        from .web import start_web
        self._web = await start_web(self.due)
        self._scheduler_task = asyncio.create_task(self._run_scheduler())  # keep a ref so it isn't GC'd

    async def close(self) -> None:
        if self._web:
            await self._web.cleanup()
        await super().close()

    async def _run_scheduler(self) -> None:
        await self.wait_until_ready()
        log.info("DUE-E online as %s — watching %d feed(s)", self.user, len(self.cfg.feeds))
        try:
            await self.scheduler.run_forever()
        except Exception:
            # Heartbeat stops here, so `due-e check` will report DOWN and FIX-E can restart us.
            log.exception("scheduler crashed")
            raise

    # ── slash commands ──────────────────────────────────────────────────────────────
    def _register_commands(self) -> None:
        due, radar, cfg = self.due, self.radar, self.cfg
        tracker = due.tracker

        async def course_autocomplete(_: discord.Interaction, current: str):
            courses = sorted({i.course for i in radar.items if i.course})
            return [app_commands.Choice(name=c, value=c) for c in courses if current.lower() in c.lower()][:25]

        async def category_autocomplete(_: discord.Interaction, current: str):
            return [app_commands.Choice(name=c, value=c) for c in tracker.category_names()
                    if current.lower() in c.lower()][:25]

        async def reply(interaction: discord.Interaction, text: str, private: bool = True):
            await interaction.response.send_message(text, ephemeral=private, allowed_mentions=NO_PINGS,
                                                    suppress_embeds=True)

        @self.tree.command(name="due", description="What's due soon across all your course calendars")
        @app_commands.describe(days="How many days ahead (1–14)", course="Only this course, e.g. EECS 281",
                               private="Only you can see the reply")
        @app_commands.autocomplete(course=course_autocomplete)
        async def due_cmd(interaction: discord.Interaction, days: app_commands.Range[int, 1, 14] = 7,
                          course: str | None = None, private: bool = False):
            await interaction.response.defer(ephemeral=private, thinking=True)
            await due.refresh()
            items = radar.upcoming(days=days, course=course)
            text = render.agenda(items, radar.now(), days, "Due soon", course=course)
            for part in render.chunk(text):
                await interaction.followup.send(part, ephemeral=private, allowed_mentions=NO_PINGS,
                                                suppress_embeds=True)

        @self.tree.command(name="log", description="Log something you did (e.g. 1h30 EECS 281 project)")
        @app_commands.describe(what="What you did", duration="45m, 1h, 1h30…", category="Leave blank to auto-detect",
                               focus="How focused, 1–5")
        @app_commands.autocomplete(category=category_autocomplete)
        async def log_cmd(interaction: discord.Interaction, what: str, duration: str, category: str | None = None,
                          focus: app_commands.Range[int, 1, 5] | None = None):
            try:
                row = tracker.log(what, minutes=_parse_minutes(duration), category=category, focus=focus,
                                  source="discord")
            except ValueError as e:
                return await reply(interaction, f"⚠ {e}")
            await reply(interaction, f"✓ Logged {_fmt_h(row['minutes'])} **{row['label']}** → {row['category']}")

        @self.tree.command(name="start", description="Start a timer")
        @app_commands.autocomplete(category=category_autocomplete)
        async def start_cmd(interaction: discord.Interaction, what: str, category: str | None = None):
            try:
                row = tracker.start_timer(what, category, source="discord")
            except ValueError as e:
                return await reply(interaction, f"⚠ {e}")
            prev = row.get("stopped_previous")
            extra = f" (stopped {prev['label']} at {_fmt_h(prev['minutes'])})" if prev else ""
            await reply(interaction, f"▶ Timer started: **{row['label']}** · {row['category']}{extra}")

        @self.tree.command(name="stop", description="Stop the running timer")
        @app_commands.describe(focus="How focused were you, 1–5")
        async def stop_cmd(interaction: discord.Interaction, focus: app_commands.Range[int, 1, 5] | None = None):
            row = tracker.stop_timer(focus)
            await reply(interaction, f"■ Stopped **{row['label']}** · {_fmt_h(row['minutes'])}" if row
                        else "No timer running.")

        @self.tree.command(name="today", description="Today's score, time breakdown and plan")
        async def today_cmd(interaction: discord.Interaction):
            an = due.analyze(1)
            t = an["totals"]
            lines = ["📊 **Today**"]
            lines.append("Nothing tracked yet." if an["score"] is None else
                         f"Score **{an['score']}**/100 · productive {_fmt_h(t['productive'])} / {_fmt_h(t['target'])}"
                         f" · waste {_fmt_h(t['waste'])}")
            for c in an["by_category"][:5]:
                lines.append(f"• {c['name']}: {_fmt_h(c['minutes'])}")
            now = due.now()
            todo = [b for b in due.planner.blocks(now.date(), now.date()) if b["status"] == "planned"
                    and b["end"] > now.isoformat()]
            if todo:
                lines.append("**Still planned:** " + " · ".join(
                    f"{b['start'][11:16]} {b['title']}" for b in todo[:6]))
            run = tracker.running()
            if run:
                lines.append(f"⏱ Running: {run['label']} since {run['start'][11:16]}")
            lines.append(f"Close out your day → {cfg.web_url}/#today")
            await reply(interaction, "\n".join(lines))

        @self.tree.command(name="status", description="DUE-E health: feeds, last refresh, next posts")
        async def status_cmd(interaction: discord.Interaction):
            lines = ["📡 **DUE-E status**"]
            for r in radar.feed_results:
                icon = "✅" if r.ok else ("🟡" if r.used_cache else "🔴")
                extra = f" — {r.error}{' (using cached copy)' if r.used_cache else ''}" if not r.ok else ""
                lines.append(f"{icon} **{r.name}** · {r.items} upcoming{extra}")
            if radar.refreshed_at:
                lines.append(f"Last refresh <t:{int(radar.refreshed_at.timestamp())}:R>")
            for name, at in self.scheduler._next.items():
                lines.append(f"Next {name} post <t:{int(at.timestamp())}:F>")
            lines.append(f"Web app: {cfg.web_url}")
            await reply(interaction, "\n".join(lines))


def post_once(channel_id: int, token: str, text: str) -> None:
    """Send one message over Discord's REST API (no gateway connection) — used by `due-e post`."""

    async def main():
        client = discord.Client(intents=discord.Intents.default())
        async with client:
            await client.login(token)
            channel = await client.fetch_channel(channel_id)
            for part in render.chunk(text):
                await channel.send(part, allowed_mentions=NO_PINGS, suppress_embeds=True)

    try:
        asyncio.run(main())
    except discord.LoginFailure:
        raise SystemExit("Discord rejected the bot token — re-copy it from the Developer Portal (Bot → Reset Token).")
    except discord.NotFound:
        raise SystemExit("Channel not found — check DUEE_CHANNEL_ID, and that the bot was invited to that server.")
    except discord.Forbidden:
        raise SystemExit("The bot can't post in that channel — give it View Channel + Send Messages there.")


def run_bot(due: DueE, token: str) -> None:
    if not due.cfg.channel_id:
        raise SystemExit("discord.channel_id is missing from config.yaml")
    DueeBot(due).run(token, log_handler=None)
