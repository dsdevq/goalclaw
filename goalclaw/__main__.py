"""Entrypoint — the systemd timer fires ``python -m goalclaw tick`` (one wakeup).

Wires the real dependencies from env config and runs one heartbeat over every
goal. The process exits when the tick is done — ephemeral body, durable mind.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from .config import Config
from .devclaw_client import HttpDevclawClient
from .goal_store import GoalStore
from .notify import HttpNotifier, NullNotifier
from .planner import claude_with_model
from .tick import tick_all


async def _run_tick(cfg: Config) -> int:
    store = GoalStore(cfg.goals_dir)
    devclaw = HttpDevclawClient(cfg.devclaw_url, cfg.devclaw_token)
    notifier = HttpNotifier(cfg.notify_url) if cfg.notify_url else NullNotifier()
    claude_caller = claude_with_model(cfg.planner_model)
    outcomes = await tick_all(
        store=store,
        devclaw=devclaw,
        claude_caller=claude_caller,
        notifier=notifier,
        notify_url=cfg.notify_url,
    )
    print(json.dumps({gid: o.value for gid, o in outcomes.items()}, indent=2))
    return 0


async def _run_loop(cfg: Config) -> int:
    """Resident heartbeat — tick every GOALCLAW_TICK_INTERVAL_SECONDS forever.

    The container CMD. Each tick is independent and idle ticks cost ~0 tokens
    (the cheap check gates the LLM), so a tight interval is cheap. State lives on
    disk, so a crash/restart just resumes — the durable-mind invariant holds even
    though the process is resident rather than timer-spawned.
    """
    interval = int(os.environ.get("GOALCLAW_TICK_INTERVAL_SECONDS", "900"))
    print(f"goalclaw loop: ticking every {interval}s over {cfg.goals_dir}", flush=True)
    while True:
        try:
            await _run_tick(cfg)
        except Exception as exc:  # noqa: BLE001 — a tick crash must not kill the loop
            print(f"tick crashed: {exc}", file=sys.stderr, flush=True)
        await asyncio.sleep(interval)


def _print_status(cfg: Config, goal_id: str | None) -> int:
    store = GoalStore(cfg.goals_dir)
    ids = [goal_id] if goal_id else store.list_goal_ids()
    if not ids:
        print("(no goals)")
        return 0
    for gid in ids:
        s = store.load_status(gid)
        inflight = f"{s.in_flight.tool}:{s.in_flight.id}" if s.in_flight else "-"
        print(f"{gid}: phase={s.phase} in_flight={inflight} next={s.next!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "tick"
    cfg = Config.from_env()
    if cmd == "tick":
        return asyncio.run(_run_tick(cfg))
    if cmd == "loop":
        return asyncio.run(_run_loop(cfg))
    if cmd == "status":
        return _print_status(cfg, args[1] if len(args) > 1 else None)
    print(f"usage: goalclaw [tick|loop|status [goal_id]]  (got {cmd!r})", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
