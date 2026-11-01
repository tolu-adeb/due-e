"""Rule-based analysis: how productive/optimal were you, and where is time leaking?

Everything here is deterministic and free. ai.py can optionally layer a Claude review on top.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from .config import Config

WEIGHTS = {"output": 35, "follow_through": 20, "attendance": 15, "deep_work": 12, "waste_control": 18}
DEEP_MIN = 45          # a productive session this long (and focused) counts as deep work
SHORT_MIN = 25         # productive sessions shorter than this are "fragments"
SWITCH_COST_MIN = 10   # rough cost of each context switch


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _norm(title: str) -> str:
    return re.sub(r"[\d#:]+", "", title.casefold()).strip()


def _part_of_day(dt: datetime) -> str:
    h = dt.hour
    return "morning" if 5 <= h < 12 else "afternoon" if h < 17 else "evening" if h < 22 else "late night"


def _fmt_h(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def analyze(activities: list[dict], confirmations: list[dict], cfg: Config,
            start_day: date, end_day: date) -> dict:
    t = cfg.tracker
    kind_of = t.kind_of
    days = _days(start_day, end_day)

    per_day = {d: defaultdict(int) for d in days}           # kind -> minutes
    by_cat: Counter = Counter()
    labels_by_kind: dict[str, Counter] = defaultdict(Counter)
    productive_sessions: list[dict] = []
    hour_minutes: Counter = Counter()
    hour_focus: dict[int, list[int]] = defaultdict(list)
    late_waste = 0
    sleep_by_day: Counter = Counter()

    for a in activities:
        d = date.fromisoformat(a["day"])
        if d not in per_day or not a.get("minutes"):
            continue
        kind = kind_of(a["category"])
        per_day[d][kind] += a["minutes"]
        by_cat[a["category"]] += a["minutes"]
        if a["category"] == "sleep":
            sleep_by_day[d] += a["minutes"]
        labels_by_kind[kind][a["label"].strip().lower()] += a["minutes"]
        start = _dt(a["start"])
        if kind == "productive":
            productive_sessions.append(a)
            # spread minutes over the hours the session covers
            cur, end = start, _dt(a["end"])
            while cur < end:
                nxt = min(end, (cur + timedelta(hours=1)).replace(minute=0, second=0))
                hour_minutes[cur.hour] += int((nxt - cur).total_seconds() // 60)
                if a.get("focus"):
                    hour_focus[cur.hour].append(a["focus"])
                cur = nxt
        if kind == "waste" and (start.hour >= 23 or start.hour < 4):
            late_waste += a["minutes"]

    logged_days = [d for d in days if sum(per_day[d].values()) > 0]
    n = len(logged_days)

    def target(d: date) -> float:
        return (t.weekend_target_hours if d.weekday() >= 5 else t.daily_target_hours) * 60

    productive = sum(per_day[d]["productive"] for d in logged_days)
    waste = sum(per_day[d]["waste"] for d in logged_days)
    deep = sum(a["minutes"] for a in productive_sessions
               if a["minutes"] >= DEEP_MIN and (a.get("focus") in (None, 4, 5)))
    target_total = sum(target(d) for d in logged_days)

    confs = [c for c in confirmations if c["status"] != "cancelled"]
    # classes are scored as attendance, not as plan follow-through (no double penalty)
    class_confs = [c for c in confs if kind_of(c.get("category") or "") == "class"]
    plan_confs = [c for c in confs if c not in class_confs]
    planned = sum(c["planned_minutes"] for c in plan_confs)
    honored = sum(min(c["actual_minutes"] or 0, c["planned_minutes"]) for c in plan_confs
                  if c["status"] in ("done", "partial"))
    class_planned = len(class_confs)
    class_attended = sum(1 for c in class_confs if c["status"] == "done") \
        + 0.5 * sum(1 for c in class_confs if c["status"] == "partial")
    class_minutes = sum(per_day[d]["class"] for d in logged_days)

    # ── score ────────────────────────────────────────────────────────────────
    comps: dict[str, float] = {}
    if n:
        comps["output"] = min(1.0, productive / target_total) if target_total else 1.0
        avg_waste = waste / n
        budget = max(1, t.waste_budget_minutes)
        comps["waste_control"] = 1.0 if avg_waste <= budget else max(0.0, 1 - (avg_waste - budget) / (2 * budget))
    if planned:
        comps["follow_through"] = honored / planned
    if class_planned:
        comps["attendance"] = class_attended / class_planned
    if productive:
        comps["deep_work"] = min(1.0, deep / productive / 0.6)  # 60%+ of productive time in deep blocks = full marks
    score = None
    if comps:
        wsum = sum(WEIGHTS[k] for k in comps)
        score = round(100 * sum(WEIGHTS[k] * v for k, v in comps.items()) / wsum)

    # ── focus hours ──────────────────────────────────────────────────────────
    focus_by_hour = []
    for h in range(24):
        if hour_minutes[h]:
            f = hour_focus.get(h)
            focus_by_hour.append({"hour": h, "minutes": hour_minutes[h],
                                  "focus": round(sum(f) / len(f), 2) if f else None})
    rated = [x for x in focus_by_hour if x["focus"] is not None and len(hour_focus[x["hour"]]) >= 2]
    if rated:
        best_hours = sorted(rated, key=lambda x: (-x["focus"], -x["minutes"]))[:3]
    else:
        best_hours = sorted(focus_by_hour, key=lambda x: -x["minutes"])[:3]
    best_hours = sorted(x["hour"] for x in best_hours)

    # ── suggestions ──────────────────────────────────────────────────────────
    sugg: list[dict] = []
    week_factor = 7 / max(1, n)

    if n:
        avg_waste = waste / n
        if avg_waste > t.waste_budget_minutes:
            top = ", ".join(f"{k} ({_fmt_h(v / n)}/day)" for k, v in labels_by_kind["waste"].most_common(3))
            save = (avg_waste - t.waste_budget_minutes) * 7
            sugg.append({"title": "Cut wasted time back to your budget",
                         "detail": f"You average {_fmt_h(avg_waste)}/day of waste vs a {t.waste_budget_minutes}m budget. "
                                   f"Biggest sinks: {top}. Put app limits on the top one or move it after your last block.",
                         "minutes_per_week": round(save), "area": "waste"})
        if late_waste:
            sugg.append({"title": "Late-night scrolling is eating sleep",
                         "detail": f"{_fmt_h(late_waste * week_factor)}/week of waste starts after 11 PM. "
                                   "Phone on the charger across the room at 11 is the cheapest fix there is.",
                         "minutes_per_week": round(late_waste * week_factor), "area": "waste"})

    if len(productive_sessions) >= 4:
        short = [a for a in productive_sessions if a["minutes"] < SHORT_MIN]
        if len(short) / len(productive_sessions) >= 0.3:
            sugg.append({"title": "Batch your fragments",
                         "detail": f"{len(short)} of {len(productive_sessions)} work sessions were under {SHORT_MIN} min. "
                                   "Each switch costs ~10 min of ramp-up — merge them into 50–90 min blocks.",
                         "minutes_per_week": round(len(short) * SWITCH_COST_MIN * week_factor), "area": "focus"})

    if class_planned and class_attended / class_planned < 0.9:
        missed = Counter(c["title"] for c in class_confs if c["status"] == "skipped")
        partial = Counter(c["title"] for c in class_confs if c["status"] == "partial")
        fmt = lambda cnt: ", ".join(k + (f" ×{v}" if v > 1 else "") for k, v in cnt.most_common(4))
        parts = ([f"skipped {fmt(missed)}"] if missed else []) + ([f"left early from {fmt(partial)}"] if partial else [])
        sugg.append({"title": f"Class attendance {round(100 * class_attended / class_planned)}% "
                              f"({class_attended:g}/{class_planned})",
                     "detail": f"You {' and '.join(parts)}. Class time is the cheapest learning you get — a missed "
                               "lecture usually costs 1.5–2× its length to self-teach later.",
                     "minutes_per_week": None, "area": "class"})

    if len(plan_confs) >= 3:
        confs = plan_confs
        skipped = [c for c in confs if c["status"] == "skipped"]
        rate = len(skipped) / len(confs)
        if rate >= 0.3:
            parts = Counter(_part_of_day(_dt(c["start"])) for c in skipped)
            part, cnt = parts.most_common(1)[0]
            repeat = [k for k, v in Counter(_norm(c["title"]) for c in skipped).items() if v >= 2]
            extra = f" You keep skipping: {', '.join(repeat[:3])} — reschedule or drop them." if repeat else ""
            sugg.append({"title": f"You skip {round(rate * 100)}% of planned blocks",
                         "detail": f"Most skips happen in the {part} ({cnt}).{extra} "
                                   "A plan you don't follow costs you twice: the planning and the guilt.",
                         "minutes_per_week": round(sum(c["planned_minutes"] for c in skipped) * week_factor),
                         "area": "planning"})

    if productive and best_hours:
        in_best = sum(hour_minutes[h] for h in best_hours)
        if in_best / productive < 0.4 and rated:
            hrs = ", ".join(f"{h % 12 or 12}{'am' if h < 12 else 'pm'}" for h in best_hours)
            sugg.append({"title": "Move hard work into your best hours",
                         "detail": f"Your focus ratings peak around {hrs}, but only {round(100 * in_best / productive)}% "
                                   "of your work happens then. DUE-E schedules prep there first.",
                         "minutes_per_week": None, "area": "focus"})

    if n:
        awake = _minutes_between(t.day_start, t.day_end)
        gaps = [awake - (sum(per_day[d].values()) - sleep_by_day[d]) for d in logged_days]
        avg_gap = sum(max(0, g) for g in gaps) / n
        if avg_gap > 180:
            sugg.append({"title": f"{_fmt_h(avg_gap)}/day is untracked",
                         "detail": "That's usually where the leaks are. Log everything for 3 days (even "
                                   "'scrolling') — the analysis gets much sharper.",
                         "minutes_per_week": None, "area": "tracking"})
        if target_total and productive / target_total < 0.7:
            gap = (target_total - productive) / n
            sugg.append({"title": f"{_fmt_h(gap)}/day short of your productive target",
                         "detail": "Add the work to the Planner as events so DUE-E puts real blocks on your calendar "
                                   "instead of hoping time appears.",
                         "minutes_per_week": None, "area": "output"})
        leisure = sum(per_day[d]["leisure"] for d in logged_days)
        if n >= 5 and leisure == 0 and productive / max(1, target_total) >= 0.8:
            sugg.append({"title": "Schedule some guilt-free time",
                         "detail": "You hit your targets with zero leisure logged. Add a habit (gaming, a show, "
                                   "friends) — planned rest beats burnout.",
                         "minutes_per_week": None, "area": "balance"})

    sugg.sort(key=lambda s: -(s["minutes_per_week"] or 0))

    daily = []
    for d in days:
        k = per_day[d]
        daily.append({"day": d.isoformat(), "productive": k["productive"], "class": k["class"], "waste": k["waste"],
                      "health": k["health"], "maintenance": k["maintenance"], "leisure": k["leisure"],
                      "target": round(target(d)), "logged": d in logged_days})

    return {
        "range": {"start": start_day.isoformat(), "end": end_day.isoformat(), "days": len(days), "logged_days": n},
        "score": score,
        "components": {k: round(v * 100) for k, v in comps.items()},
        "totals": {"productive": productive, "waste": waste, "deep": deep, "target": round(target_total),
                   "planned": planned, "honored": honored, "class": class_minutes,
                   "classes_planned": class_planned, "classes_attended": class_attended},
        "by_category": [{"name": c, "kind": kind_of(c), "minutes": m, "per_day": round(m / max(1, n))}
                        for c, m in by_cat.most_common()],
        "top_wasters": [{"label": k, "minutes": v} for k, v in labels_by_kind["waste"].most_common(5)],
        "daily": daily,
        "focus_by_hour": focus_by_hour,
        "best_hours": best_hours,
        "suggestions": sugg,
        # suggestions in the same area overlap (late-night scrolling is part of total waste), so count each area once
        "recoverable_minutes_per_week": sum(
            max((s["minutes_per_week"] or 0) for s in sugg if s["area"] == area) for area in {s["area"] for s in sugg}),
    }


def _minutes_between(a, b) -> int:
    m = (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)
    return m if m > 0 else m + 24 * 60


def summary_text(an: dict, style: str = "discord") -> str:
    """Short text version for Discord posts."""
    b = (lambda s: f"**{s}**") if style == "discord" else (lambda s: s)
    t = an["totals"]
    if an["score"] is None:
        return "Nothing tracked yet — log a few things or confirm your day to get a score."
    lines = [f"{b('Score')} {an['score']}/100 · productive {_fmt_h(t['productive'])} of {_fmt_h(t['target'])} target"
             f" · waste {_fmt_h(t['waste'])}"]
    if t["planned"]:
        lines.append(f"Plan follow-through {round(100 * t['honored'] / t['planned'])}%")
    if t["classes_planned"]:
        lines.append(f"Classes {t['classes_attended']:g}/{t['classes_planned']} attended · {_fmt_h(t['class'])} in class")
    for s in an["suggestions"][:3]:
        gain = f" (≈{_fmt_h(s['minutes_per_week'])}/wk)" if s["minutes_per_week"] else ""
        lines.append(f"• {s['title']}{gain}")
    return "\n".join(lines)
