"""Runtime config — all from env, with sane local-dev defaults.

Mirrors devclaw's "env picks the tier / the endpoint / the token" convention so
the two services configure the same way.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


@dataclass(frozen=True)
class Config:
    #: root holding one folder per goal: <goals_dir>/<goal_id>/
    goals_dir: Path
    #: devclaw streamable-http MCP endpoint (sibling container in prod)
    devclaw_url: str
    #: bearer token guarding devclaw's HTTP transport ("" → no auth, local dev)
    devclaw_token: str
    #: notify-relay endpoint; POSTed a JSON body on goal transitions ("" → off)
    notify_url: str
    #: claude --model tier for the plan step. Bounded JSON emission → sonnet.
    planner_model: str | None
    #: claude binary
    claude_bin: str
    #: plan-step wall-clock budget
    planner_timeout_ms: int

    @staticmethod
    def from_env() -> "Config":
        return Config(
            goals_dir=_expand(os.environ.get("GOALCLAW_GOALS_DIR", "~/memory/goals")),
            devclaw_url=os.environ.get("DEVCLAW_URL", "http://127.0.0.1:8000/mcp"),
            devclaw_token=os.environ.get("DEVCLAW_TOKEN", ""),
            notify_url=os.environ.get("GOALCLAW_NOTIFY_URL", ""),
            planner_model=os.environ.get("GOALCLAW_PLANNER_MODEL", "sonnet") or None,
            claude_bin=os.environ.get("GOALCLAW_CLAUDE_BIN", "claude"),
            planner_timeout_ms=int(os.environ.get("GOALCLAW_PLANNER_TIMEOUT_MS", "90000")),
        )
