from datetime import datetime, timedelta

from duee.analysis import analyze, summary_text

from .conftest import NOW, TZ

DAY = NOW.date()


def act(label, cat, minutes, hour, day=DAY, focus=None):
    s = datetime.combine(day, datetime.min.time(), tzinfo=TZ) + timedelta(hours=hour)
    e = s + timedelta(minutes=minutes)
    return {"label": label, "category": cat, "minutes": minutes, "start": s.isoformat(), "end": e.isoformat(),
            "day": day.isoformat(), "focus": focus}


def run(cfg, acts, confs=(), days=1):
    return analyze(list(acts), list(confs), cfg, DAY - timedelta(days=days - 1), DAY)


def test_nothing_tracked(cfg):
    an = run(cfg, [])
    assert an["score"] is None and an["suggestions"] == []
    assert "Nothing tracked" in summary_text(an)


def test_perfect_day_scores_high(cfg):
    acts = [act("EECS 281 project", "study", 120, 10, focus=5), act("EECS 370 hw", "study", 120, 14, focus=4),
            act("reading", "study", 120, 19, focus=4)]
    an = run(cfg, acts)  # Monday target is 6h
    assert an["score"] == 100 and an["totals"]["deep"] == 360


def test_waste_suggestions_and_no_double_counting(cfg):
    acts = [act("study", "study", 120, 10), act("tiktok", "scrolling", 150, 21), act("youtube", "scrolling", 60, 23.5)]
    an = run(cfg, acts)
    titles = [s["title"] for s in an["suggestions"]]
    assert "Cut wasted time back to your budget" in titles
    assert "Late-night scrolling is eating sleep" in titles
    waste = [s["minutes_per_week"] for s in an["suggestions"] if s["area"] == "waste"]
    assert an["recoverable_minutes_per_week"] == max(waste)  # overlapping waste advice counted once
    assert an["top_wasters"][0] == {"label": "tiktok", "minutes": 150}
    assert an["components"]["waste_control"] < 100


def test_fragmentation(cfg):
    acts = [act(f"hw {i}", "study", 15, 9 + i) for i in range(5)] + [act("project", "study", 90, 16)]
    titles = [s["title"] for s in run(cfg, acts)["suggestions"]]
    assert "Batch your fragments" in titles


def test_follow_through_and_skip_pattern(cfg):
    def conf(title, hour, status, mins=60):
        s = datetime.combine(DAY, datetime.min.time(), tzinfo=TZ) + timedelta(hours=hour)
        return {"title": title, "start": s.isoformat(), "planned_minutes": mins, "status": status,
                "actual_minutes": mins if status == "done" else 0}
    confs = [conf("Gym", 7, "skipped"), conf("Gym 2", 7.5, "skipped"), conf("Study", 14, "done"),
             conf("Lecture", 10, "done"), conf("Old meeting", 16, "cancelled")]
    an = run(cfg, [act("x", "study", 30, 14)], confs)
    assert an["components"]["follow_through"] == 50  # cancelled items don't count against you
    skip = next(s for s in an["suggestions"] if s["area"] == "planning")
    assert "morning" in skip["detail"] and "gym" in skip["detail"]


def test_best_hours_from_focus(cfg):
    acts = []
    for d in range(4):
        day = DAY - timedelta(days=d)
        acts += [act("deep", "study", 60, 10, day, focus=5), act("meh", "study", 60, 21, day, focus=2)]
    an = run(cfg, acts, days=4)
    assert an["best_hours"][0] == 10
