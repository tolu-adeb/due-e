"""The task reader: looks at a deadline (title, course, Canvas description) and works out what it is.

Three layers, each optional on top of the last:
  1. Rules — keyword/quantity reading of the text ("Project 2", "1500 words", "problems 1–12",
     "autograder", "worth 20%") → kind, effort, difficulty, plus the reasons it decided that.
  2. Your pace — once you've logged time on finished tasks, estimates are scaled by how long
     things *actually* take you, per course and kind (see `calibrate`).
  3. Claude (optional, ai.read_tasks) — reads the full description for a second opinion.

Every read keeps human-readable `signals`, so the Planner can show *why* it planned what it did.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

import requests

# kind → (base minutes of work, deep work?)
KIND_BASE = {
    "exam": (480, True), "quiz": (90, False), "project": (600, True), "essay": (240, True),
    "presentation": (180, True), "lab": (120, False), "reading": (60, False), "assignment": (180, True),
}

# First match wins: title first, then the description. "not_task" = things you attend, not do.
KIND_RULES: list[tuple[str, re.Pattern]] = [(k, re.compile(p, re.I)) for k, p in [
    ("not_task", r"\b(office hours?|lecture|review session|discussion section|recitation|lab section|seminar|"
                 r"no class|holiday|(spring|fall|winter|thanksgiving) break|study day|reading day)\b"),
    ("exam", r"\b(midterm|final exam|exam|prelim)\b"),
    ("quiz", r"\bquiz(zes)?\b"),
    ("project", r"\b(project|proj|capstone|milestone|p[1-9])\b"),
    ("essay", r"\b(essay|paper|write-?up|report|memo|thesis)\b"),
    ("presentation", r"\b(presentation|slides|pitch|poster session)\b"),
    ("lab", r"\b(pre-?lab|post-?lab|lab)\b"),
    ("reading", r"\b(reading|response|reflection|discussion post|journal|survey|annotation|read)\b"),
    ("assignment", r"\b(homework|hw|problem set|pset|ps\d+|assignment|worksheet|exercises?|written)\b"),
]]

# (pattern, multiplier, reason)
MODIFIERS: list[tuple[re.Pattern, float, str]] = [(re.compile(p, re.I), m, r) for p, m, r in [
    (r"\b(implement|autograder|code|coding|program|c\+\+|python|debug|test cases)\b", 1.15, "coding work"),
    (r"\b(group|team|partner)\b", 0.9, "shared with a group"),
    (r"\b(final project|final exam|cumulative|comprehensive)\b", 1.4, "final / cumulative"),
    (r"\b(milestone|checkpoint|part [a-d1-9]|phase \d)\b", 0.6, "one part of a bigger deliverable"),
    (r"\b(optional|extra credit|bonus)\b", 0.5, "optional / extra credit"),
    (r"\b(short|brief|quick|mini)\b", 0.6, "described as short"),
    (r"\b(proof|prove|derive|derivation)\b", 1.2, "proofs / derivations"),
    (r"\b(research|sources|citations|bibliography)\b", 1.2, "research needed"),
]]

COURSE_CODE = re.compile(r"\b([A-Z]{2,10})\s*(\d{3})\b")


@dataclass
class TaskRead:
    kind: str | None                 # None → not a task (lecture, office hours…)
    est_minutes: int                 # raw estimate, before your personal pace
    difficulty: int                  # 1–5
    deep: bool                       # needs long focused blocks?
    confidence: float                # 0–1
    summary: str = ""
    signals: list[str] = field(default_factory=list)
    source: str = "rules"            # rules | claude

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | None) -> "TaskRead | None":
        if not raw:
            return None
        try:
            return cls(**json.loads(raw))
        except (TypeError, ValueError):
            return None


def _first_sentence(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    m = re.match(r"(.{20,}?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _fmt(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h{m:02d}" if h and m else (f"{h}h" if h else f"{m}m")


def read(title: str, course: str | None = None, description: str = "", all_day: bool = False) -> TaskRead:
    """Rule-based read of one deadline."""
    text = f"{title}\n{description}"
    signals: list[str] = []
    confidence = 0.3

    kind = None
    for k, pat in KIND_RULES:
        m = pat.search(title)
        if m:
            kind = k
            signals.append(f"title says “{m.group(0)}” → {k.replace('_', ' ')}")
            confidence += 0.3
            break
    if kind is None and description:
        for k, pat in KIND_RULES:
            if k == "not_task":
                continue
            m = pat.search(description)
            if m:
                kind = k
                signals.append(f"description mentions “{m.group(0)}” → {k}")
                confidence += 0.15
                break
    if kind == "not_task":
        return TaskRead(None, 0, 1, False, round(min(confidence, 1), 2), "", signals)
    if kind is None:
        kind = "assignment"
        signals.append("no clear type → treated as an assignment")

    base, deep = KIND_BASE[kind]
    minutes = float(base)
    explicit = None

    # explicit quantities beat the base guess
    if m := re.search(r"(\d{3,5})\s*(-|to|–)?\s*(\d{3,5})?\s*-?\s*words?\b", text, re.I):
        words = int(m.group(3) or m.group(1))
        explicit = words / 250 * 60  # ~250 polished words/hour incl. editing
        signals.append(f"{words} words → ~{_fmt(explicit)} of writing")
    elif m := re.search(r"(\d{1,3})\s*(-|to|–)?\s*(\d{1,3})?\s*pages?", text, re.I):
        pages = int(m.group(3) or m.group(1))
        per = 90 if kind == "essay" else 5
        explicit = pages * per
        signals.append(f"{pages} pages → ~{_fmt(explicit)}")
    elif m := re.search(r"(problems?|questions?|exercises?|parts?)\s*#?\s*(\d{1,2})\s*(-|to|–|through)\s*#?\s*(\d{1,2})",
                        text, re.I):
        n = abs(int(m.group(4)) - int(m.group(2))) + 1
        explicit = n * (20 if kind != "quiz" else 6)
        signals.append(f"{n} {m.group(1).lower().rstrip('s')}s → ~{_fmt(explicit)}")
    elif m := re.search(r"chapters?\s*(\d{1,2})\s*(-|to|–|through|and)\s*(\d{1,2})", text, re.I):
        n = abs(int(m.group(3)) - int(m.group(1))) + 1
        explicit = n * (90 if kind == "exam" else 45)
        signals.append(f"{n} chapters → ~{_fmt(explicit)}")
    if explicit:
        minutes = max(explicit, base * 0.25)
        confidence += 0.2

    for pat, mult, why in MODIFIERS:
        if pat.search(text):
            minutes *= mult
            signals.append(f"{why} ×{mult:g}")

    if m := re.search(r"(\d{1,4})\s*(points|pts)\b", text, re.I):
        pts = int(m.group(1))
        if pts >= 100:
            minutes *= 1.2
            signals.append(f"{pts} points ×1.2")
        elif pts <= 10:
            minutes *= 0.6
            signals.append(f"only {pts} points ×0.6")
    if m := re.search(r"(\d{1,2}(?:\.\d)?)\s*%\s*(of\s+(your|the)\s+)?(final\s+)?grade", text, re.I):
        pct = float(m.group(1))
        mult = 1.3 if pct >= 15 else (0.6 if pct <= 3 else 1.0)
        if mult != 1.0:
            minutes *= mult
            signals.append(f"worth {pct:g}% of the grade ×{mult:g}")

    words = len(description.split())
    if words > 1000:
        minutes *= 1.3
        signals.append(f"long spec ({words} words) ×1.3")
    elif words > 400:
        minutes *= 1.15
        signals.append(f"detailed spec ({words} words) ×1.15")
    if description:
        confidence += 0.1

    ratio = minutes / base
    difficulty = max(1, min(5, round(3 + 2 * math.log2(ratio)))) if ratio > 0 else 3
    deep = deep and minutes >= 75
    est = int(round(minutes / 15) * 15) or 15
    summary = _first_sentence(description) if description else ""
    if not summary:  # nothing from Canvas → say what the reader thinks it is
        what = {"exam": "exam to study for", "quiz": "quiz to review for", "reading": "reading"}.get(kind, kind)
        summary = f"{(course + ' ') if course else ''}{what} · ~{_fmt(est)} · {'deep work' if deep else 'light work'}"
    return TaskRead(kind, est, difficulty, deep, round(min(confidence, 1.0), 2), summary, signals)


# ── personal pace ──────────────────────────────────────────────────────────────
def calibrate(history: list[dict], course: str | None, kind: str, prior: float = 3.0) -> tuple[float, str | None]:
    """How long things really take you vs. the reader's estimate.

    history: finished tasks as {course, kind, est_minutes, actual_minutes}. Uses the geometric mean of
    actual/estimate, shrunk toward 1.0 with `prior` pseudo-observations so a single odd week can't
    swing it. Tries course+kind, then course, then kind.
    """
    def pace(rows):
        logs = [math.log(max(0.2, min(5.0, r["actual_minutes"] / r["est_minutes"])))
                for r in rows if r.get("est_minutes") and r.get("actual_minutes", 0) >= 15]
        if len(logs) < 2:
            return None
        return math.exp(sum(logs) / (len(logs) + prior)), len(logs)

    course_key = _course_key(course)
    for rows, label in (
        ([r for r in history if _course_key(r["course"]) == course_key and r["kind"] == kind], f"{course} {kind}s"),
        ([r for r in history if _course_key(r["course"]) == course_key], f"{course}"),
        ([r for r in history if r["kind"] == kind], f"{kind}s"),
    ):
        if course_key is None and label.startswith(str(course)):
            continue
        got = pace(rows)
        if got:
            mult, n = got
            mult = max(0.4, min(2.5, mult))
            if abs(mult - 1) < 0.05:
                return 1.0, None
            return mult, f"your pace on {label}: ×{mult:.2f} (from {n} finished)"
    return 1.0, None


def _course_key(course: str | None) -> str | None:
    if not course:
        return None
    m = COURSE_CODE.search(course.upper())
    return f"{m.group(1)} {m.group(2)}" if m else course.upper().strip()


# ── matching logged time to a task ─────────────────────────────────────────────
_STOP = {"the", "a", "an", "and", "for", "on", "of", "to", "in", "work", "working", "do", "doing", "study",
         "studying", "hw", "homework", "assignment"}


_ABBR = {"p": "project", "proj": "project", "pa": "project", "prj": "project", "q": "quiz", "mt": "midterm",
         "ex": "exam", "ps": "problem", "pset": "problem", "lab": "lab", "l": "lab", "ch": "chapter", "ess": "essay"}


def _tokens(s: str) -> set[str]:
    # "p2" → project 2, "hw5" → hw 5, "eecs281" → eecs 281
    s = re.sub(r"\b([a-z]{1,5})(\d{1,3})\b", r"\1 \2", s.lower())
    return {_ABBR.get(t, t) for t in re.findall(r"[a-z0-9]+", s) if t not in _STOP}


def match_score(label: str, title: str, course: str | None) -> float:
    """How likely a logged label ("281 project 2") refers to a task ("Project 2", EECS 281)."""
    lt = _tokens(label)
    if not lt:
        return 0.0
    tt = _tokens(title)
    ct = _tokens(course or "")
    hit_title = len((lt - ct) & tt) / max(1, len(tt))
    hit_course = 1.0 if (lt & ct) else 0.0
    numbers_l = {t for t in lt if t.isdigit()} - ct   # "281" in "281 project 2" is the course, not the number
    numbers_t = {t for t in tt if t.isdigit()} - ct
    if numbers_l and numbers_t and not (numbers_l & numbers_t):
        return 0.0  # "project 3" is not "project 2"
    return 0.7 * hit_title + 0.3 * hit_course


# ── optional Claude read ───────────────────────────────────────────────────────
AI_PROMPT = (
    "You estimate student workload. Given one course deliverable, reply with ONLY a JSON object: "
    '{"kind": one of ["exam","quiz","project","essay","presentation","lab","reading","assignment","not_task"], '
    '"est_hours": number (focused hours for a typical sophomore CS student to finish/prepare), '
    '"difficulty": 1-5, "summary": "one sentence: what the work actually is", '
    '"why": ["short reason", ...]}'
)


def read_with_claude(cfg, title: str, course: str | None, description: str, timeout: float = 30) -> TaskRead | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not (cfg.ai.enabled and cfg.ai.read_tasks and key):
        return None
    body = f"Course: {course or 'unknown'}\nTitle: {title}\nDescription:\n{description[:3500] or '(none)'}"
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": cfg.ai.model, "max_tokens": 300, "system": AI_PROMPT,
                  "messages": [{"role": "user", "content": body}]},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        text = "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (requests.RequestException, ValueError, KeyError):
        return None
    kind = data.get("kind")
    if kind == "not_task":
        return TaskRead(None, 0, 1, False, 0.8, data.get("summary", ""), ["Claude: not a task"], "claude")
    if kind not in KIND_BASE:
        return None
    minutes = max(15, min(60 * 60, float(data.get("est_hours", 1)) * 60))
    return TaskRead(kind, int(round(minutes / 15) * 15), max(1, min(5, int(data.get("difficulty", 3)))),
                    KIND_BASE[kind][1] and minutes >= 75, 0.8, str(data.get("summary", ""))[:200],
                    [f"Claude: {w}" for w in data.get("why", [])][:5], "claude")


def merged_read(cfg, title: str, course: str | None, description: str, all_day: bool = False) -> TaskRead:
    """Rules always; Claude on top when enabled (its kind/estimate win, rule signals are kept)."""
    rules = read(title, course, description, all_day)
    smart = read_with_claude(cfg, title, course, description) if rules.kind is not None or description else None
    if not smart:
        return rules
    if smart.kind is None:
        return smart
    smart.signals = smart.signals + [f"rules thought: {rules.kind}, ~{_fmt(rules.est_minutes)}"]
    return smart


def now_iso(tz) -> str:
    return datetime.now(tz).isoformat(timespec="seconds")
