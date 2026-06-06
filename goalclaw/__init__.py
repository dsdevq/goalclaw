"""GoalClaw — the goal-layer planner.

The orchestration above the engines: a durable *goal* (objective + state on
disk) advanced across heartbeat wakeups. Each tick does a cheap, zero-token
deterministic check first; only when there is real work does it spend an LLM to
decide the next action, then dispatches that action to a domain engine (devclaw
for code). Ephemeral body, durable mind: the process dies between ticks; the
goal persists as files under ~/memory/goals/<id>/.
"""

__version__ = "0.0.1"
