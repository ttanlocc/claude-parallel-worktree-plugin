#!/usr/bin/env python3
"""assert-based checks for the assignment ledger. Run: python3 bin/test_assignments.py"""

import os as _os
import tempfile
from datetime import UTC, datetime

from assignments import (
    OPEN_STATUSES,
    at_risk,
    new_assignment,
    open_assignments,
    progress,
)
from escalations import append, current_state

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC).timestamp()
PAST = "2026-08-27"
FUTURE = "2026-08-30"


def test_new_assignment_defaults():
    a = new_assignment("Ship the manager")
    assert a["title"] == "Ship the manager"
    assert a["priority"] == "P1"
    assert a["status"] == "assigned"
    assert a["plan"] == []
    assert a["ado_refs"] == []
    assert len(a["id"]) == 12


def test_new_assignment_rejects_blank_title():
    for bad in ("", "   "):
        try:
            new_assignment(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for title {bad!r}")


def test_new_assignment_rejects_unknown_priority():
    try:
        new_assignment("x", priority="P9")
    except ValueError:
        return
    raise AssertionError("expected ValueError for priority 'P9'")


def test_at_risk_when_deadline_passed():
    a = new_assignment("late", deadline=PAST)
    assert at_risk(a, NOW) is True


def test_at_risk_false_when_deadline_ahead():
    a = new_assignment("fine", deadline=FUTURE)
    assert at_risk(a, NOW) is False


def test_at_risk_on_a_late_step():
    a = new_assignment("stepwise")
    a["plan"] = [{"step": "s", "owner": "w", "depends_on": [], "eta": PAST, "state": "doing"}]
    assert at_risk(a, NOW) is True


def test_at_risk_ignores_a_late_step_already_done():
    a = new_assignment("stepwise")
    a["plan"] = [{"step": "s", "owner": "w", "depends_on": [], "eta": PAST, "state": "done"}]
    assert at_risk(a, NOW) is False


def test_at_risk_false_when_closed():
    for status in ("done", "cancelled"):
        a = new_assignment("closed", deadline=PAST)
        a["status"] = status
        assert at_risk(a, NOW) is False, status


def test_at_risk_false_without_dates():
    assert at_risk(new_assignment("undated"), NOW) is False


def test_deadline_today_is_not_yet_late():
    """A date-only deadline means the end of that day, not midnight."""
    a = new_assignment("today", deadline="2026-08-28")
    assert at_risk(a, NOW) is False


def test_unparseable_date_never_raises_an_alarm():
    a = new_assignment("garbled", deadline="not-a-date")
    assert at_risk(a, NOW) is False


def test_progress_none_without_a_plan():
    assert progress(new_assignment("unplanned")) is None


def test_progress_counts_done_steps():
    a = new_assignment("half")
    a["plan"] = [{"state": "done"}, {"state": "doing"}, {"state": "todo"}, {"state": "done"}]
    assert progress(a) == 0.5


def test_progress_full():
    a = new_assignment("all")
    a["plan"] = [{"state": "done"}, {"state": "done"}]
    assert progress(a) == 1.0


def test_ledger_folds_to_the_latest_state():
    fd, path = tempfile.mkstemp()
    _os.close(fd)
    try:
        a = new_assignment("evolving")
        append(path, a)
        append(path, {**a, "status": "in_progress", "note": "started"})
        state = current_state(path)
        assert len(state) == 1
        assert state[0]["status"] == "in_progress"
        assert state[0]["note"] == "started"
    finally:
        _os.unlink(path)


def test_open_assignments_excludes_closed():
    fd, path = tempfile.mkstemp()
    _os.close(fd)
    try:
        live = new_assignment("live")
        gone = new_assignment("gone")
        gone["status"] = "done"
        append(path, live)
        append(path, gone)
        names = [a["title"] for a in open_assignments(path)]
        assert names == ["live"], names
        assert all(s in OPEN_STATUSES for s in ("assigned", "in_progress", "blocked"))
    finally:
        _os.unlink(path)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
