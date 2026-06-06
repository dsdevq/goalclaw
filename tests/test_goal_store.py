"""Durable-mind round-trips and cadence math."""

from __future__ import annotations

import pytest

from goalclaw.goal_store import GoalStore, parse_duration
from goalclaw.models import GoalStatus, InFlight
from tests._fakes import Clock, seed_goal


def test_parse_duration():
    assert parse_duration("90s") == 90
    assert parse_duration("30m") == 1800
    assert parse_duration("6h") == 21600
    assert parse_duration("1d") == 86400
    with pytest.raises(ValueError):
        parse_duration("nonsense")


def test_load_goal(tmp_path):
    seed_goal(tmp_path, "g1", backlog=["x", "y"])
    store = GoalStore(tmp_path)
    g = store.load_goal("g1")
    assert g.id == "g1"
    assert g.engine == "devclaw"
    assert g.workspace_dir == "/repos/demo"
    assert g.backlog == ["x", "y"]
    assert g.open_pr is True


def test_status_roundtrip(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    s = GoalStatus(
        phase="in_flight",
        in_flight=InFlight("devclaw", "start_program", "prog_1", "program", "do the thing"),
        next="do the thing",
        last_plan_at="2026-06-06T12:00:00+00:00",
        inbox_cursor=2,
    )
    store.save_status("g1", s)
    back = store.load_status("g1")
    assert back.phase == "in_flight"
    assert back.in_flight is not None
    assert back.in_flight.id == "prog_1"
    assert back.in_flight.ref_kind == "program"
    assert back.inbox_cursor == 2


def test_missing_status_is_default(tmp_path):
    store = GoalStore(tmp_path)
    s = store.load_status("never")
    assert s.phase == "idle"
    assert s.in_flight is None


def test_log_append_and_recent(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    store.append_log("g1", "first")
    store.append_log("g1", "second")
    recent = store.recent_log("g1")
    assert "first" in recent and "second" in recent
    assert recent.index("first") < recent.index("second")  # newest at bottom


def test_inbox_cursor(tmp_path):
    d = tmp_path / "g1"
    d.mkdir(parents=True)
    (d / "inbox.md").write_text("# steering\n\nfocus on auth first\n")
    store = GoalStore(tmp_path)
    s0 = store.load_status("g1")  # cursor 0
    assert store.unread_steering("g1", s0) == "focus on auth first"
    # after consuming
    cursor = store.steering_cursor("g1")
    assert cursor == 1
    s1 = GoalStatus(inbox_cursor=cursor)
    assert store.unread_steering("g1", s1) == ""
    # new steering appended
    with (d / "inbox.md").open("a") as fh:
        fh.write("also add metrics\n")
    assert store.unread_steering("g1", s1) == "also add metrics"


def test_cadence_due(tmp_path):
    clock = Clock()
    store = GoalStore(tmp_path, now=clock)
    seed_goal(tmp_path, "g1", cadence="6h")
    goal = store.load_goal("g1")
    # never planned → due
    assert store.cadence_due(goal, GoalStatus(last_plan_at=None)) is True
    # planned just now → not due
    just_now = store.now_iso()
    assert store.cadence_due(goal, GoalStatus(last_plan_at=just_now)) is False
    # advance past cadence → due
    clock.advance(6 * 3600 + 1)
    assert store.cadence_due(goal, GoalStatus(last_plan_at=just_now)) is True
