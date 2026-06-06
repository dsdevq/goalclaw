"""The durable mind on disk — reusing the vault projects/ convention.

Layout per goal, under <goals_dir>/<goal_id>/:
  goal.yaml   FACTS    — objective, cadence, engine, workspace_dir, done_when, backlog
  STATUS.md   STATE    — machine state in YAML frontmatter, overwritten each tick
  log.md      EVENTS   — append-only, newest at bottom
  inbox.md    STEERING — append-only direction from Denys; consumed by cursor

No database: the filesystem IS the store, git-synced like the rest of the vault.
A clock is injected (``now``) so ticks are deterministic under test.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from .models import Goal, GoalStatus, InFlight

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_DURATION = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s: str) -> int:
    """'6h' / '1d' / '30m' / '90s' → seconds. Raises ValueError on garbage."""
    m = _DURATION.match(s or "")
    if not m:
        raise ValueError(f"bad cadence {s!r}; want <int><s|m|h|d>")
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2)]


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


class GoalStore:
    def __init__(self, goals_dir: Path, *, now: Callable[[], datetime] = _default_now) -> None:
        self._root = Path(goals_dir)
        self._now = now

    # ---- discovery ---------------------------------------------------------

    def list_goal_ids(self) -> list[str]:
        if not self._root.exists():
            return []
        return sorted(p.name for p in self._root.iterdir() if (p / "goal.yaml").is_file())

    def _dir(self, goal_id: str) -> Path:
        return self._root / goal_id

    # ---- goal (facts) ------------------------------------------------------

    def load_goal(self, goal_id: str) -> Goal:
        raw = yaml.safe_load((self._dir(goal_id) / "goal.yaml").read_text()) or {}
        return Goal(
            id=goal_id,
            objective=str(raw["objective"]).strip(),
            cadence=str(raw.get("cadence", "1d")),
            engine=raw.get("engine", "devclaw"),
            workspace_dir=str(raw["workspace_dir"]),
            verify_cmd=raw.get("verify_cmd") or None,
            open_pr=bool(raw.get("open_pr", True)),
            done_when=str(raw.get("done_when", "")).strip(),
            backlog=[str(x).strip() for x in (raw.get("backlog") or [])],
        )

    # ---- status (state) ----------------------------------------------------

    def load_status(self, goal_id: str) -> GoalStatus:
        path = self._dir(goal_id) / "STATUS.md"
        if not path.exists():
            return GoalStatus()
        fm = self._read_frontmatter(path.read_text())
        inflight = None
        if fm.get("in_flight"):
            f = fm["in_flight"]
            inflight = InFlight(
                engine=f["engine"], tool=f["tool"], id=f["id"],
                ref_kind=f["ref_kind"], goal=f.get("goal", ""),
            )
        return GoalStatus(
            phase=fm.get("phase", "idle"),
            in_flight=inflight,
            blocked_on=fm.get("blocked_on") or None,
            next=fm.get("next", "") or "",
            last_plan_at=fm.get("last_plan_at") or None,
            last_tick_at=fm.get("last_tick_at") or None,
            inbox_cursor=int(fm.get("inbox_cursor", 0)),
        )

    def save_status(self, goal_id: str, status: GoalStatus) -> None:
        fm: dict = {
            "phase": status.phase,
            "in_flight": (
                {
                    "engine": status.in_flight.engine,
                    "tool": status.in_flight.tool,
                    "id": status.in_flight.id,
                    "ref_kind": status.in_flight.ref_kind,
                    "goal": status.in_flight.goal,
                }
                if status.in_flight
                else None
            ),
            "blocked_on": status.blocked_on,
            "next": status.next,
            "last_plan_at": status.last_plan_at,
            "last_tick_at": status.last_tick_at,
            "inbox_cursor": status.inbox_cursor,
        }
        body = self._render_status_body(goal_id, status)
        text = "---\n" + yaml.safe_dump(fm, sort_keys=False).rstrip() + "\n---\n\n" + body
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "STATUS.md").write_text(text)

    # ---- log (events) ------------------------------------------------------

    def append_log(self, goal_id: str, message: str) -> None:
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "log.md"
        if not path.exists():
            path.write_text(f"# {goal_id} — log\n\n")
        with path.open("a") as fh:
            fh.write(f"- [{self._now().isoformat(timespec='seconds')}] {message}\n")

    def recent_log(self, goal_id: str, n: int = 20) -> str:
        path = self._dir(goal_id) / "log.md"
        if not path.exists():
            return ""
        lines = [ln for ln in path.read_text().splitlines() if ln.startswith("- [")]
        return "\n".join(lines[-n:])

    # ---- inbox (steering) --------------------------------------------------

    def _inbox_lines(self, goal_id: str) -> list[str]:
        path = self._dir(goal_id) / "inbox.md"
        if not path.exists():
            return []
        out = []
        for ln in path.read_text().splitlines():
            s = ln.strip()
            if s and not s.startswith("#"):
                out.append(s)
        return out

    def unread_steering(self, goal_id: str, status: GoalStatus) -> str:
        lines = self._inbox_lines(goal_id)
        fresh = lines[status.inbox_cursor :]
        return "\n".join(fresh).strip()

    def steering_cursor(self, goal_id: str) -> int:
        return len(self._inbox_lines(goal_id))

    # ---- helpers -----------------------------------------------------------

    def cadence_due(self, goal: Goal, status: GoalStatus) -> bool:
        if status.last_plan_at is None:
            return True
        try:
            last = datetime.fromisoformat(status.last_plan_at)
        except ValueError:
            return True
        return (self._now() - last).total_seconds() >= parse_duration(goal.cadence)

    def now_iso(self) -> str:
        return self._now().isoformat(timespec="seconds")

    @staticmethod
    def _read_frontmatter(text: str) -> dict:
        m = _FRONTMATTER.match(text)
        if not m:
            return {}
        return yaml.safe_load(m.group(1)) or {}

    @staticmethod
    def _render_status_body(goal_id: str, s: GoalStatus) -> str:
        if s.phase == "in_flight" and s.in_flight:
            head = f"running `{s.in_flight.tool}` ({s.in_flight.id})"
        elif s.phase == "blocked":
            head = f"blocked — {s.blocked_on}"
        else:
            head = s.phase
        lines = [
            f"# {goal_id} — status",
            "",
            f"**phase:** {head}",
        ]
        if s.next:
            lines.append(f"**next:** {s.next}")
        if s.last_tick_at:
            lines.append(f"\n_updated {s.last_tick_at}_")
        return "\n".join(lines) + "\n"
