"""The heartbeat loop — one wakeup.

Order is load-bearing: the cheap, deterministic, ZERO-TOKEN check runs first and
short-circuits when there's nothing to do. The plan step (the only LLM call)
runs ONLY past that gate. This is the quota guardrail — N idle ticks must cost
~0 tokens, or the Pro weekly quota dies (burned this way 2026-05-18).

Everything is injected (store, devclaw, claude_caller, notifier) so a whole tick
runs deterministically under test with no network and no claude.
"""

from __future__ import annotations

from dataclasses import replace
from enum import Enum
from typing import Awaitable, Callable

from . import planner as _planner
from .devclaw_client import DevclawClient
from .goal_store import GoalStore
from .models import Goal, GoalStatus
from .notify import Notifier
from .planner import ClaudeCaller
from .workspace import WorkspaceError, prepare_workspace

#: (workspace_dir, repo_url) -> default branch. Injected so tests pass a no-op.
WorkspacePrep = Callable[[str, "str | None"], Awaitable[str]]


class Outcome(str, Enum):
    IDLE = "idle"            # cheap check found nothing — 0 tokens
    IN_FLIGHT = "in_flight"  # dispatched action still running — 0 tokens
    DISPATCHED = "dispatched"
    SLEPT = "slept"
    BLOCKED = "blocked"
    DONE = "done"
    SKIP_DONE = "skip_done"
    ERROR = "error"


async def tick_goal(
    goal_id: str,
    *,
    store: GoalStore,
    devclaw: DevclawClient,
    claude_caller: ClaudeCaller,
    notifier: Notifier,
    notify_url: str = "",
    prepare_ws: WorkspacePrep = prepare_workspace,
) -> Outcome:
    goal = store.load_goal(goal_id)
    status = store.load_status(goal_id)
    if status.phase == "done":
        return Outcome.SKIP_DONE

    # ---- cheap check (zero tokens) ----------------------------------------
    finished_detail = ""
    if status.in_flight is not None:
        poll = await devclaw.poll(status.in_flight)
        if poll.running:
            store.save_status(goal_id, replace(status, last_tick_at=store.now_iso()))
            return Outcome.IN_FLIGHT
        # terminal → record DELIVERY EVIDENCE (PR url + gate) so the planner can
        # see the item shipped and mark it done — without this it re-dispatches.
        evidence = []
        if poll.pr_url:
            evidence.append(f"PR {poll.pr_url}")
        if poll.gate_passed is not None:
            evidence.append("gate=passed" if poll.gate_passed else "gate=FAILED")
        ev_str = (" — " + ", ".join(evidence)) if evidence else ""
        store.append_log(
            goal_id, f"{status.in_flight.tool} {status.in_flight.id} → {poll.status}{ev_str}"
        )
        finished_detail = (
            f"tool={status.in_flight.tool} id={status.in_flight.id} "
            f"status={poll.status}{ev_str}\n{poll.detail}"
        )
        status = replace(status, in_flight=None, phase="idle")

    steering = store.unread_steering(goal_id, status)
    work = bool(finished_detail) or bool(steering)
    if status.phase == "blocked":
        should_plan = work  # cadence does NOT re-poke a blocked goal; only a human/finish unblocks
    else:
        should_plan = work or store.cadence_due(goal, status)

    if not should_plan:
        store.save_status(goal_id, replace(status, last_tick_at=store.now_iso()))
        return Outcome.IDLE

    # ---- plan (the only LLM call) + act -----------------------------------
    try:
        result = await _planner.plan(
            goal, status, store.recent_log(goal_id), steering, finished_detail,
            claude_caller=claude_caller,
        )
    except _planner.PlannerError as exc:
        store.append_log(goal_id, f"plan error: {exc}")
        store.save_status(goal_id, replace(status, last_tick_at=store.now_iso()))
        await notifier.send(f"⚠️ [{goal_id}] plan step failed: {exc}")
        return Outcome.ERROR

    now = store.now_iso()
    base = replace(
        status,
        last_plan_at=now,
        last_tick_at=now,
        inbox_cursor=store.steering_cursor(goal_id),  # all current steering consumed
    )

    if result.decision == "sleep":
        store.save_status(goal_id, replace(base, phase="idle", next=result.note))
        store.append_log(goal_id, f"sleep: {result.note}")
        return Outcome.SLEPT

    if result.decision == "blocked":
        store.save_status(goal_id, replace(base, phase="blocked", blocked_on=result.question, next=""))
        store.append_log(goal_id, f"blocked: {result.question}")
        await notifier.send(f"🟡 [{goal_id}] needs you — {result.question}")
        return Outcome.BLOCKED

    if result.decision == "done":
        store.save_status(goal_id, replace(base, phase="done", next=result.note))
        store.append_log(goal_id, f"done: {result.note}")
        await notifier.send(f"✅ [{goal_id}] goal complete — {result.note}")
        return Outcome.DONE

    # decision == "act"
    action = result.actions[0]
    # Runaway backstop (mechanism, not cognition): never spawn more than
    # backlog-size + a small margin of engine actions for one goal without a
    # human. A looping planner can't burn unbounded quota — it blocks instead.
    cap = len(goal.backlog) + 2
    if base.actions_dispatched >= cap:
        store.append_log(goal_id, f"dispatch cap {cap} reached — blocking for review")
        store.save_status(
            goal_id,
            replace(base, phase="blocked", blocked_on=f"dispatch cap {cap} reached — review the open PRs"),
        )
        await notifier.send(f"🛑 [{goal_id}] dispatch cap ({cap}) reached — paused for your review")
        return Outcome.BLOCKED
    # Give the engine a pristine checkout at latest origin/default — so this
    # action doesn't pile onto a previous action's branch (per-action freshness).
    try:
        await prepare_ws(goal.workspace_dir, goal.repo_url)
    except WorkspaceError as exc:
        store.append_log(goal_id, f"workspace prep failed: {exc}")
        store.save_status(goal_id, replace(base, phase="idle", next=action.goal))
        await notifier.send(f"⚠️ [{goal_id}] workspace prep failed: {exc}")
        return Outcome.ERROR
    try:
        ref = await devclaw.dispatch(action, goal, notify_url)
    except Exception as exc:  # noqa: BLE001 — record + notify, retry next cadence
        store.append_log(goal_id, f"dispatch error ({action.tool}): {exc}")
        store.save_status(goal_id, replace(base, phase="idle", next=action.goal))
        await notifier.send(f"⚠️ [{goal_id}] dispatch failed: {exc}")
        return Outcome.ERROR
    store.save_status(
        goal_id,
        replace(
            base, phase="in_flight", in_flight=ref, blocked_on=None, next=action.goal,
            actions_dispatched=base.actions_dispatched + 1,
        ),
    )
    store.append_log(goal_id, f"dispatched {action.tool}: {action.goal} → {ref.id}")
    await notifier.send(f"🚀 [{goal_id}] {action.tool}: {action.goal}  ({ref.id})")
    return Outcome.DISPATCHED


async def tick_all(
    *,
    store: GoalStore,
    devclaw: DevclawClient,
    claude_caller: ClaudeCaller,
    notifier: Notifier,
    notify_url: str = "",
    prepare_ws: WorkspacePrep = prepare_workspace,
) -> dict[str, Outcome]:
    """Tick every goal. One goal's failure never stops the others."""
    outcomes: dict[str, Outcome] = {}
    for goal_id in store.list_goal_ids():
        try:
            outcomes[goal_id] = await tick_goal(
                goal_id,
                store=store,
                devclaw=devclaw,
                claude_caller=claude_caller,
                notifier=notifier,
                notify_url=notify_url,
                prepare_ws=prepare_ws,
            )
        except Exception:  # noqa: BLE001 — isolate per-goal blast radius
            store.append_log(goal_id, "tick crashed (uncaught)")
            outcomes[goal_id] = Outcome.ERROR
    return outcomes
