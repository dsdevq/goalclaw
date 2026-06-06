"""The heartbeat loop — including the load-bearing zero-token guardrail.

The single most important assertions in this repo: an idle tick and an
in-flight-still-running tick must leave ``FakeClaude.calls == 0``. If those ever
go non-zero, the Pro quota dies under N idle ticks/day.
"""

from __future__ import annotations

import json

import pytest

from goalclaw.goal_store import GoalStore
from goalclaw.models import GoalStatus, InFlight, PollResult
from goalclaw.tick import Outcome, tick_goal
from tests._fakes import Clock, FakeClaude, FakeDevclaw, RecordingNotifier, fake_prepare, seed_goal

ACT = json.dumps(
    {"decision": "act", "note": "ship next", "actions": [{"tool": "start_program", "goal": "build /health"}]}
)


def _store(tmp_path, clock):
    return GoalStore(tmp_path, now=clock)


async def _tick(store, goal_id, claude, devclaw, notifier):
    return await tick_goal(
        goal_id, store=store, devclaw=devclaw, claude_caller=claude, notifier=notifier,
        notify_url="http://relay", prepare_ws=fake_prepare,
    )


# ---- the guardrail ---------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_tick_spends_zero_tokens(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g", cadence="1d")
    store.save_status("g", GoalStatus(phase="idle", last_plan_at=store.now_iso()))
    claude, devclaw, notifier = FakeClaude(ACT), FakeDevclaw(), RecordingNotifier()

    out = await _tick(store, "g", claude, devclaw, notifier)

    assert out is Outcome.IDLE
    assert claude.calls == 0          # <-- the quota guardrail
    assert devclaw.dispatched == []
    assert notifier.sent == []


@pytest.mark.asyncio
async def test_in_flight_running_spends_zero_tokens(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g")
    store.save_status(
        "g",
        GoalStatus(phase="in_flight", in_flight=InFlight("devclaw", "start_program", "p1", "program")),
    )
    claude = FakeClaude(ACT)
    devclaw = FakeDevclaw(poll_result=PollResult(terminal=False, status="running"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", claude, devclaw, notifier)

    assert out is Outcome.IN_FLIGHT
    assert claude.calls == 0          # <-- still zero while the engine works
    assert devclaw.polls == 1


# ---- the working path ------------------------------------------------------


@pytest.mark.asyncio
async def test_first_tick_plans_and_dispatches(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g")  # no STATUS yet → cadence due → plan
    claude, devclaw, notifier = FakeClaude(ACT), FakeDevclaw(), RecordingNotifier()

    out = await _tick(store, "g", claude, devclaw, notifier)

    assert out is Outcome.DISPATCHED
    assert claude.calls == 1
    assert len(devclaw.dispatched) == 1
    action, goal, notify_url = devclaw.dispatched[0]
    assert action.tool == "start_program"
    assert notify_url == "http://relay"
    saved = store.load_status("g")
    assert saved.phase == "in_flight"
    assert saved.in_flight is not None
    assert any("start_program" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_workspace_prepped_before_dispatch(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g")
    claude, devclaw, notifier = FakeClaude(ACT), FakeDevclaw(), RecordingNotifier()
    calls: list[tuple] = []

    async def rec_prepare(ws, repo_url=None):
        calls.append((ws, repo_url))
        return "main"

    out = await tick_goal(
        "g", store=store, devclaw=devclaw, claude_caller=claude, notifier=notifier,
        notify_url="", prepare_ws=rec_prepare,
    )
    assert out is Outcome.DISPATCHED
    assert calls == [("/repos/demo", None)]  # prepped the goal's workspace, once
    assert len(devclaw.dispatched) == 1      # then dispatched


@pytest.mark.asyncio
async def test_idle_tick_does_not_prep_workspace(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g", cadence="1d")
    store.save_status("g", GoalStatus(phase="idle", last_plan_at=store.now_iso()))
    claude, devclaw, notifier = FakeClaude(ACT), FakeDevclaw(), RecordingNotifier()
    calls: list = []

    async def rec_prepare(ws, repo_url=None):
        calls.append(ws)
        return "main"

    out = await tick_goal(
        "g", store=store, devclaw=devclaw, claude_caller=claude, notifier=notifier,
        prepare_ws=rec_prepare,
    )
    assert out is Outcome.IDLE
    assert calls == []  # no work → no workspace churn


@pytest.mark.asyncio
async def test_finished_action_feeds_next_plan(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g")
    store.save_status(
        "g",
        GoalStatus(phase="in_flight", in_flight=InFlight("devclaw", "start_program", "p1", "program", "first")),
    )
    claude = FakeClaude(ACT)
    devclaw = FakeDevclaw(poll_result=PollResult(terminal=True, status="done", detail="{\"pr\":\"#7\"}"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", claude, devclaw, notifier)

    assert out is Outcome.DISPATCHED
    assert claude.calls == 1
    assert "done" in claude.last_prompt          # finished result fed to planner
    assert len(devclaw.dispatched) == 1


@pytest.mark.asyncio
async def test_steering_triggers_plan_even_when_cadence_not_due(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g", cadence="1d")
    store.save_status("g", GoalStatus(phase="idle", last_plan_at=store.now_iso()))
    (tmp_path / "g" / "inbox.md").write_text("pause features, fix the failing CI first\n")
    claude, devclaw, notifier = FakeClaude(ACT), FakeDevclaw(), RecordingNotifier()

    out = await _tick(store, "g", claude, devclaw, notifier)

    assert out is Outcome.DISPATCHED
    assert claude.calls == 1
    assert "failing CI" in claude.last_prompt
    assert store.load_status("g").inbox_cursor == 1   # steering consumed


# ---- blocked + terminal decisions -----------------------------------------


@pytest.mark.asyncio
async def test_blocked_goal_stays_idle_without_steering(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g", cadence="1h")
    # planned long ago → cadence WOULD be due, but blocked must not re-plan
    store.save_status("g", GoalStatus(phase="blocked", blocked_on="which DB?", last_plan_at="2026-06-01T00:00:00+00:00"))
    claude, devclaw, notifier = FakeClaude(ACT), FakeDevclaw(), RecordingNotifier()

    out = await _tick(store, "g", claude, devclaw, notifier)

    assert out is Outcome.IDLE
    assert claude.calls == 0          # blocked goals don't burn tokens on cadence


@pytest.mark.asyncio
async def test_blocked_goal_resumes_on_steering(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="blocked", blocked_on="which DB?"))
    (tmp_path / "g" / "inbox.md").write_text("use postgres\n")
    claude, devclaw, notifier = FakeClaude(ACT), FakeDevclaw(), RecordingNotifier()

    out = await _tick(store, "g", claude, devclaw, notifier)

    assert out is Outcome.DISPATCHED
    assert claude.calls == 1


@pytest.mark.asyncio
async def test_pr_evidence_logged_on_done(tmp_path):
    # The fix for the re-dispatch loop: a finished task's PR url + gate must land
    # in the log so the planner sees the item shipped.
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g")
    store.save_status(
        "g", GoalStatus(phase="in_flight", in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "x")),
    )
    devclaw = FakeDevclaw(
        poll_result=PollResult(
            terminal=True, status="done", pr_url="https://github.com/o/r/pull/9", gate_passed=True
        )
    )
    claude = FakeClaude(json.dumps({"decision": "done", "note": "shipped"}))
    notifier = RecordingNotifier()

    await _tick(store, "g", claude, devclaw, notifier)

    recent = store.recent_log("g")
    assert "PR https://github.com/o/r/pull/9" in recent
    assert "gate=passed" in recent
    assert "PR https://github.com/o/r/pull/9" in claude.last_prompt  # planner sees it too


@pytest.mark.asyncio
async def test_dispatch_cap_blocks_runaway(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g")  # backlog 2 → cap = 4
    store.save_status("g", GoalStatus(phase="idle", actions_dispatched=4))  # already at cap
    claude, devclaw, notifier = FakeClaude(ACT), FakeDevclaw(), RecordingNotifier()

    out = await _tick(store, "g", claude, devclaw, notifier)

    assert out is Outcome.BLOCKED
    assert devclaw.dispatched == []  # refused to spawn another run
    assert store.load_status("g").phase == "blocked"
    assert any("cap" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_planner_done(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g")
    claude = FakeClaude(json.dumps({"decision": "done", "note": "all backlog merged"}))
    devclaw, notifier = FakeDevclaw(), RecordingNotifier()

    out = await _tick(store, "g", claude, devclaw, notifier)

    assert out is Outcome.DONE
    assert store.load_status("g").phase == "done"
    assert any("complete" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_planner_blocked_notifies(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g")
    claude = FakeClaude(json.dumps({"decision": "blocked", "question": "which auth provider?"}))
    devclaw, notifier = FakeDevclaw(), RecordingNotifier()

    out = await _tick(store, "g", claude, devclaw, notifier)

    assert out is Outcome.BLOCKED
    s = store.load_status("g")
    assert s.phase == "blocked"
    assert s.blocked_on == "which auth provider?"
    assert any("auth provider" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_done_goal_is_skipped(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="done"))
    claude, devclaw, notifier = FakeClaude(ACT), FakeDevclaw(), RecordingNotifier()

    out = await _tick(store, "g", claude, devclaw, notifier)

    assert out is Outcome.SKIP_DONE
    assert claude.calls == 0
