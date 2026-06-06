# AGENTS.md — goalclaw

Repo comprehension for autonomous agents (and humans). GoalClaw is a **core
system**: code shape is the felt experience. Prefer clarity over cleverness.

## What this is

The goal-layer planner that sits ABOVE devclaw. A durable goal (`~/memory/goals/<id>/`)
advanced across heartbeat wakeups. devclaw is the code *engine*; goalclaw is the
*orchestrator* that decides the next action and dispatches it. Read `README.md`
for the model.

## Stack / layout

- Python ≥3.11, async. Deps: `fastmcp` (devclaw HTTP MCP client), `httpx`
  (notify), `pyyaml` (state files). Build: hatchling.
- Package `goalclaw/`:
  - `config.py` — env → `Config` (frozen dataclass).
  - `models.py` — `Goal` (facts) / `GoalStatus` (state) / `Action` / `PlanResult` / `InFlight` / `PollResult`. All frozen dataclasses.
  - `goal_store.py` — `GoalStore`: the durable mind on disk (goal.yaml, STATUS.md frontmatter, log.md, inbox.md). Injectable `now` clock.
  - `devclaw_client.py` — `HttpDevclawClient` + the `DevclawClient` Protocol. The engine seam.
  - `planner.py` — the plan step (cognition). `plan()` takes an injected `claude_caller`; `validate()` is the JSON contract. Mirrors devclaw's planner.
  - `notify.py` — `HttpNotifier` / `NullNotifier`.
  - `workspace.py` — `prepare_workspace`: pristine checkout of the repo's default branch at latest origin (clone-if-missing, else fetch+hard-reset+clean) before each code action. goalclaw owns the goal↔repo↔workspace lifecycle; devclaw just receives a ready workspace.
  - `wake.py` — tiny `POST /wake` HTTP server (daemon thread). devclaw's notify_url points here → task-done triggers an immediate tick (event), not just the heartbeat.
  - `tick.py` — `tick_goal` / `tick_all`. **The heart.** Cheap-check-first ordering is load-bearing. Preps the workspace (injected `prepare_ws`) before dispatching a code action.
  - `__main__.py` — `python -m goalclaw tick|serve|status`. `serve` = heartbeat + /wake event loop (`wait(wake OR interval) → tick`); the container CMD.

## Build / run / test (the verify gate)

```bash
pip install -e ".[dev]"
python -m pytest -q          # <-- THE GATE. Must stay green. ~30 fast unit tests, no network/claude.
```

Live integration (optional, needs a running devclaw): see `tests/test_integration_devclaw.py`.

## Invariants — do not regress

1. **Zero-token idle path.** `tick_goal` MUST run the cheap deterministic check
   (poll in-flight status / inbox cursor / cadence) BEFORE any LLM call. An idle
   tick and an in-flight-still-running tick spend **0** claude calls. The tests
   `test_idle_tick_spends_zero_tokens` and `test_in_flight_running_spends_zero_tokens`
   guard this — never weaken them. (Quota was burned this way 2026-05-18.)
2. **Mechanism / cognition split.** Python owns the loop, check, dispatch, state,
   notify. Claude is called ONLY to decide the next action and returns JSON that
   `planner.validate()` checks. Don't push reasoning into Python or mechanism
   into the prompt.
3. **One action in flight per goal.** The planner returns exactly one action;
   the engine runs it to a reviewable PR before the next is chosen. `validate()`
   rejects multi-action plans.
4. **goalclaw is not an engine.** It never writes code / runs the agent loop.
   New capability belongs in an engine behind the `DevclawClient`-style seam, not
   inline here.
5. **No new store.** State is files under the vault. No database.
6. **Blocked goals don't burn cadence.** A blocked goal re-plans only on new
   steering or a finished action — never on the cadence timer.

## Testing conventions

- `tests/_fakes.py`: `FakeClaude` (counts calls — the quota assertion),
  `FakeDevclaw`, `RecordingNotifier`, `Clock` (advanceable), `seed_goal`.
- Everything in `tick_goal` is injected, so a whole tick runs with no network and
  no claude. Keep it that way — don't introduce module-level singletons.
