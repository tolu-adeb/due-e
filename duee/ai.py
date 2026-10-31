"""Optional weekly review from Claude, layered on top of the rule-based analysis.

Off unless `ai.enabled: true` and ANTHROPIC_API_KEY is set. Only aggregates are sent (category totals,
scores, top time sinks, upcoming events) — never your notes or raw calendar.
"""

from __future__ import annotations

import json
import os

import requests

from .config import Config

API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM = (
    "You are a blunt, practical productivity coach for a university CS student. "
    "You get a week of time-tracking aggregates and their upcoming deadlines. "
    "Give 4-6 bullet points: concrete changes for next week (what to cut, move, or protect), "
    "each with an estimated time saved or gained. Reference the actual numbers. "
    "Be honest when something is going badly; don't pad with praise. No intro or outro."
)


class AiUnavailable(RuntimeError):
    pass


def available(cfg: Config) -> bool:
    return cfg.ai.enabled and bool(os.environ.get("ANTHROPIC_API_KEY"))


def build_payload(analysis: dict, events: list[dict], habits: list[dict]) -> dict:
    return {
        "range": analysis["range"],
        "score": analysis["score"],
        "score_components_pct": analysis["components"],
        "totals_minutes": analysis["totals"],
        "minutes_by_category": {c["name"]: c["minutes"] for c in analysis["by_category"]},
        "top_time_sinks": analysis["top_wasters"],
        "best_focus_hours": analysis["best_hours"],
        "rule_based_suggestions": [s["title"] for s in analysis["suggestions"]],
        "daily_productive_minutes": {d["day"]: d["productive"] for d in analysis["daily"] if d["logged"]},
        "upcoming_events": [{"title": e["title"], "kind": e["kind"], "at": e["at"],
                             "prep_needed_h": e.get("prep", {}).get("needed_hours"),
                             "prep_scheduled_h": e.get("prep", {}).get("scheduled_hours")} for e in events[:10]],
        "habits": [{"name": h["name"], "per_week": h["per_week"], "done_this_week": h.get("done_this_week")}
                   for h in habits],
    }


def weekly_review(cfg: Config, analysis: dict, events: list[dict], habits: list[dict], timeout: float = 60) -> str:
    if not cfg.ai.enabled:
        raise AiUnavailable("AI review is off — set ai.enabled: true in config.yaml")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AiUnavailable("ANTHROPIC_API_KEY is not set")
    payload = build_payload(analysis, events, habits)
    resp = requests.post(
        API_URL,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": cfg.ai.model,
            "max_tokens": cfg.ai.max_tokens,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": "My week:\n```json\n" + json.dumps(payload, indent=1) + "\n```"}],
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise AiUnavailable(f"Claude API returned HTTP {resp.status_code}")
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
