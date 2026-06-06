# GoalClaw

The **goal-layer planner** — the orchestration *above* the engines.

Where [devclaw](https://github.com/dsdevq/devclaw) executes one discrete coding
task ("fix X → PR"), GoalClaw holds a **durable goal** and advances it across
many heartbeat wakeups: it decides the next action, dispatches it to a domain
engine (devclaw for code), records what happened, notifies you, and accepts
steering in between. It is the generalized "goal + state → next action" loop —
domain-agnostic; code is just the first engine.

## The model: ephemeral body, durable mind

The process dies between ticks (cheap, crash-safe). The *goal* persists as files
on disk under `~/memory/goals/<id>/`. "Continuous" means the goal advances
*across* wakeups — not a process that never sleeps.

```
 systemd timer (heartbeat) ─┐
 inbox.md   (steering)      ├─► GoalClaw tick ─► dispatch ─► engines
 notify-relay → Telegram ◄──┘                              └─ devclaw (code)
        state: ~/memory/goals/<id>/
```

## One tick

```
load goal.yaml + STATUS.md
── cheap check (Python, ZERO tokens) ──
   in-flight? poll devclaw  → running ⇒ exit (nothing to do)
                            → terminal ⇒ feed result to the plan step
   else: new steering in inbox.md? cadence due (and not blocked)?  → plan, else exit
── plan (Claude Agent SDK, ONLY past the gate) ──
   goal + state + history + steering + finished-result → JSON {act|sleep|blocked|done}
── dispatch ── route the one action to its engine (devclaw MCP), record the id
── persist ── overwrite STATUS.md, append log.md
── notify ── notify-relay → Telegram on transitions
```

**The load-bearing rule:** the cheap, deterministic check runs *first* and costs
**zero tokens**. The LLM is spent only when there is real work. N idle ticks/day
must cost ~0 — otherwise the Claude Pro weekly quota dies. This is the
mechanism/cognition split applied to wakeups: Python owns the loop, the check,
dispatch, state, and notify; Claude only *decides the next action*.

## Goal state (`~/memory/goals/<id>/`)

| File | Role | Written |
|---|---|---|
| `goal.yaml` | FACTS — objective, cadence, engine, workspace_dir, done_when, backlog | by hand |
| `STATUS.md` | STATE — phase / in-flight ref / blocked_on / cursors (frontmatter) | overwritten each tick |
| `log.md` | EVENTS — append-only, newest at bottom | by the tick |
| `inbox.md` | STEERING — append-only direction; consumed by a line cursor | by you / Telegram |

See `examples/lifekit-dashboard.goal.yaml` for a goal definition.

## Run

```bash
pip install -e ".[dev]"
python -m goalclaw tick            # one heartbeat over every goal (one-shot)
python -m goalclaw loop            # resident heartbeat — tick every GOALCLAW_TICK_INTERVAL_SECONDS (the container CMD)
python -m goalclaw status          # print each goal's phase
```

Deployed as a small always-on container (mirrors `lifekit-curator`) running
`goalclaw loop`. Idle ticks cost ~0 tokens, so a tight interval is cheap; state
lives on disk, so a restart just resumes. (`tick` stays one-shot, so a
systemd-timer model is a trivial swap.)

Config is env (see `goalclaw/config.py`): `GOALCLAW_GOALS_DIR`, `DEVCLAW_URL`,
`DEVCLAW_TOKEN`, `GOALCLAW_NOTIFY_URL`, `GOALCLAW_PLANNER_MODEL` (default
`sonnet`).

## Test

```bash
python -m pytest -q                # unit — no network, no claude
# live integration against a running devclaw (stub mode):
GOALCLAW_IT_DEVCLAW_URL=http://127.0.0.1:8000/mcp python -m pytest tests/test_integration_devclaw.py -q
```

## Boundaries (why GoalClaw is thin)

- **Not an engine.** It never writes code or runs the agent loop — devclaw does.
  GoalClaw is the PM above the PM: it picks the next goal-sized move and hands it
  down. (Orthogonality: the general goal layer must not couple to the code domain.)
- **No new store.** State is files under the vault, git-synced like everything else.
- **No always-on process.** A systemd timer is the heartbeat; the process exits
  after each tick.
