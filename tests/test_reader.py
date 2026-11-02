from duee import reader as rd


def test_kinds_and_not_tasks():
    assert rd.read("Midterm Exam", "EECS 370").kind == "exam"
    assert rd.read("Quiz 8", "PHYSICS 240").kind == "quiz"
    assert rd.read("Project 2", "EECS 281").kind == "project"
    assert rd.read("Lab 6 Prelab", "PHYSICS 240").kind == "lab"
    assert rd.read("Homework 5", "EECS 370").kind == "assignment"
    assert rd.read("Reflection essay", "ENGLISH 125").kind == "essay"
    for t in ("Office Hours", "Lecture 14", "Exam review session"):
        assert rd.read(t, "EECS 281").kind is None, t


def test_estimates_follow_the_signals():
    plain = rd.read("Homework 5", "EECS 370")
    final = rd.read("Final Exam (cumulative)", "EECS 370")
    mid = rd.read("Midterm Exam", "EECS 370")
    assert final.est_minutes > mid.est_minutes
    assert mid.deep and final.deep
    # explicit quantities beat the base guess
    short = rd.read("Essay", "ENGLISH 125", "Write a 500-word response.")
    long = rd.read("Essay", "ENGLISH 125", "Write a 3000-word paper.")
    assert long.est_minutes > short.est_minutes * 3
    probs = rd.read("Homework 5", "EECS 370", "Do problems 1-12.")
    assert probs.est_minutes >= 12 * 20 * 0.9
    assert any("12 problems" in s or "problems" in s for s in probs.signals)
    # optional/milestone work is lighter
    assert rd.read("Project 2 milestone", "EECS 281").est_minutes < rd.read("Project 2", "EECS 281").est_minutes
    assert plain.signals and plain.summary


def test_reads_html_descriptions_and_round_trips():
    r = rd.read("Project 3", "EECS 281", "Implement in C++. Work in groups of 2. Worth 15% of your grade.")
    assert r.kind == "project"
    back = rd.TaskRead.from_json(r.to_json())
    assert back == r
    assert rd.TaskRead.from_json(None) is None


def test_calibrate_learns_your_pace():
    assert rd.calibrate([], "EECS 281", "project")[0] == 1.0
    slow = [{"course": "EECS 281", "kind": "project", "est_minutes": 600, "actual_minutes": 1200}] * 4
    m, why = rd.calibrate(slow, "EECS 281", "project")
    assert 1.4 < m < 2.0 and why  # shrunk toward 1 but clearly slower
    # one data point isn't a pattern yet
    assert rd.calibrate(slow[:1], "EECS 281", "project")[0] == 1.0
    # other courses fall back to the kind
    m2, _ = rd.calibrate(slow, "EECS 370", "project")
    assert 1.0 < m2 <= m


def test_match_score():
    assert rd.match_score("281 project 2", "Project 2", "EECS 281") >= 0.6
    assert rd.match_score("study 281 p2", "Project 2", "EECS 281") >= 0.5
    assert rd.match_score("370 hw5", "Homework 5", "EECS 370") >= 0.5
    assert rd.match_score("gym", "Project 2", "EECS 281") < 0.3
    assert rd.match_score("281 project 2", "Homework 5", "EECS 370") < 0.5
