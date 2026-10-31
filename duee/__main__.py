"""DUE-E CLI.

  python -m duee preview [--kind morning|evening|weekly|due] [--now 2026-11-02T08:00] [--raw]
  python -m duee run [--dry-run]          # bot + scheduler + web app
  python -m duee web                       # just the web app (no Discord)
  python -m duee check [--json] [--feeds-only]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime

from . import __version__, render
from .config import Config, load_config, load_dotenv
from .app import DueE
from .radar import Radar
from .scheduler import Scheduler, StdoutPoster, build_post
from .status import LEVELS, evaluate, read_status


def _clock(cfg: Config, now: str | None):
    if not now:
        return None
    fixed = datetime.fromisoformat(now)
    if fixed.tzinfo is None:
        fixed = fixed.replace(tzinfo=cfg.timezone)
    return lambda: fixed


def cmd_preview(cfg: Config, args) -> int:
    due = DueE.create(cfg, mode="preview", clock=_clock(cfg, args.now))
    radar = due.radar
    radar.refresh(force=True)
    style: render.Style = "discord" if args.raw else "plain"
    if args.kind == "due":
        items = radar.upcoming(days=args.days, course=args.course)
        text = render.agenda(items, radar.now(), args.days, "Due soon", style, course=args.course)
    else:
        text, _ = build_post(args.kind, radar, cfg, style, due)
    print(text)
    bad = [r for r in radar.feed_results if not r.ok]
    for r in bad:
        print(f"\n⚠ feed '{r.name}': {r.error}{' (used cached copy)' if r.used_cache else ''}", file=sys.stderr)
    return 1 if bad else 0


def cmd_run(cfg: Config, args) -> int:
    if args.dry_run:
        due = DueE.create(cfg, mode="dry-run")
        sched = Scheduler(cfg, due.radar, StdoutPoster(), due.status, style="plain", due=due)

        async def main():
            from .web import start_web
            runner = await start_web(due)
            if runner:
                print(f"Web app: {cfg.web_url}")
            await sched.run_forever()

        print("DUE-E dry-run: posts will print here instead of Discord. Ctrl+C to stop.")
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
        return 0

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN is not set (put it in .env). Use --dry-run to test without Discord.", file=sys.stderr)
        return 2
    from .bot import run_bot  # import discord only when needed

    run_bot(DueE.create(cfg, mode="live"), token)
    return 0


def cmd_post(cfg: Config, args) -> int:
    """Send one post to Discord right now (great for testing the connection)."""
    token = os.environ.get("DISCORD_TOKEN")
    if not token or not cfg.channel_id:
        print("Set DISCORD_TOKEN and DUEE_CHANNEL_ID in .env first.", file=sys.stderr)
        return 2
    due = DueE.create(cfg, mode="post")
    due.radar.refresh(force=True)
    if args.kind == "test":
        feeds = "\n".join(f"{'✅' if r.ok else '🔴'} {r.name} · {r.items} item(s)" + (f" — {r.error}" if r.error else "")
                          for r in due.radar.feed_results)
        text = (f"📡 **DUE-E is connected.**\n{feeds}\n"
                f"Next: morning post {cfg.morning:%H:%M}, evening {cfg.evening:%H:%M}. Try `/due` and `/today`.")
    else:
        text, _ = build_post(args.kind, due.radar, cfg, "discord", due)
    from .bot import post_once

    post_once(cfg.channel_id, token, text)
    print(f"Posted {args.kind} message to channel {cfg.channel_id}.")
    return 0


def cmd_web(cfg: Config, args) -> int:
    from .web import start_web

    due = DueE.create(cfg, mode="web")
    cfg.web.enabled = True

    async def main():
        await due.refresh(force=True)
        await start_web(due)
        print(f"DUE-E web app → {cfg.web_url}   (Ctrl+C to stop)")
        while True:
            await asyncio.sleep(3600)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_check(cfg: Config, args) -> int:
    radar = Radar(cfg)
    radar.refresh(force=True)
    status = None if args.feeds_only else read_status(cfg.status_file)
    level, reasons = evaluate(status, radar.now(), radar.feed_results, check_bot=not args.feeds_only)

    report = {
        "app": "due-e",
        "version": __version__,
        "level": LEVELS[level],
        "code": level,
        "reasons": reasons,
        "upcoming_items": len(radar.upcoming(days=14)),
        "feeds": [r.__dict__ for r in radar.feed_results],
        "status": status,
    }
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"DUE-E {__version__}: {LEVELS[level].upper()}  ({len(radar.upcoming(days=14))} upcoming items)")
        for r in radar.feed_results:
            print(f"  {'✓' if r.ok else '✗'} {r.name}: {r.items} items" + (f" — {r.error}" if r.error else ""))
        for why in reasons:
            print(f"  • {why}")
    return level


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="due-e", description="DUE-E — deadline radar for your course calendars")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"due-e {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("preview", help="print a post to the terminal (test mode)")
    pv.add_argument("--kind", choices=["morning", "evening", "weekly", "due"], default="morning")
    pv.add_argument("--days", type=int, default=7, help="for --kind due")
    pv.add_argument("--course", help="for --kind due, e.g. 'EECS 281'")
    pv.add_argument("--now", help="pretend it's this time, e.g. 2026-11-02T08:00")
    pv.add_argument("--raw", action="store_true", help="show the exact Discord markdown")

    rn = sub.add_parser("run", help="run the bot")
    rn.add_argument("--dry-run", action="store_true", help="run the schedule but print instead of posting")

    sub.add_parser("web", help="run only the local web app (tracker, planner, habits, insights)")

    po = sub.add_parser("post", help="send one post to Discord now (use --kind test to check the connection)")
    po.add_argument("--kind", choices=["test", "morning", "evening", "weekly"], default="test")

    ck = sub.add_parser("check", help="health check for VERA (exit 0 ok / 1 degraded / 2 down)")
    ck.add_argument("--json", action="store_true")
    ck.add_argument("--feeds-only", action="store_true", help="only test the feeds, ignore the running bot")

    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles: never crash on an emoji
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.INFO if args.verbose else logging.WARNING)
    # DUE-E never uses voice, so discord.py's "PyNaCl/davey not installed" warnings are just noise
    logging.getLogger("discord.client").setLevel(logging.INFO if args.verbose else logging.ERROR)

    load_dotenv()
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(f"No {args.config} found — copy config.example.yaml to config.yaml and add your feeds.", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    return {"preview": cmd_preview, "run": cmd_run, "web": cmd_web, "post": cmd_post,
            "check": cmd_check}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
