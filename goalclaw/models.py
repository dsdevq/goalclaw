"""Domain types — the durable mind, as plain data.

A Goal is the immutable-ish objective (read from goal.yaml). A GoalStatus is the
mutable point-in-time state (STATUS.md frontmatter), overwritten each tick. An
Action is a single engine call the planner decided on; a PlanResult is the whole
decision (act / sleep / blocked / done).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Engine = Literal["devclaw"]
DevclawTool = Literal["start_program", "implement_feature", "fix_bug", "review_repository"]
Phase = Literal["idle", "in_flight", "blocked", "done"]
Decision = Literal["act", "sleep", "blocked", "done"]


@dataclass(frozen=True)
class Goal:
    """The durable objective. Read from <goal_id>/goal.yaml; treated as facts."""

    id: str
    objective: str
    #: heartbeat cadence to re-plan even with no event, e.g. "6h", "1d"
    cadence: str
    engine: Engine
    workspace_dir: str
    #: git URL of the target repo — goalclaw clones it if workspace_dir is empty,
    #: and resets to its default branch before each action. None → must pre-exist.
    repo_url: Optional[str] = None
    #: gate command devclaw runs after the agent ("the agent's done is not trusted")
    verify_cmd: Optional[str] = None
    #: when True, devclaw delivers each change as a PR to review
    open_pr: bool = True
    #: prose statement of completion, evaluated by the planner
    done_when: str = ""
    #: concrete starting work-list the planner draws the next action from
    backlog: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InFlight:
    """A reference to an action devclaw is currently running for this goal."""

    engine: Engine
    tool: DevclawTool
    #: devclaw's task_id or program_id
    id: str
    #: "task" | "program" — which devclaw endpoint to poll
    ref_kind: Literal["task", "program"]
    goal: str = ""


@dataclass(frozen=True)
class GoalStatus:
    """Mutable per-tick state — STATUS.md frontmatter. Overwritten, never appended."""

    phase: Phase = "idle"
    in_flight: Optional[InFlight] = None
    blocked_on: Optional[str] = None
    #: human note of the intended next step
    next: str = ""
    #: ISO ts of the last time the plan step (LLM) ran
    last_plan_at: Optional[str] = None
    #: ISO ts of the last tick (cheap or not)
    last_tick_at: Optional[str] = None
    #: number of inbox.md lines already consumed as steering
    inbox_cursor: int = 0


@dataclass(frozen=True)
class Action:
    """One engine call the planner chose."""

    engine: Engine
    tool: DevclawTool
    goal: str
    verify_cmd: Optional[str] = None
    open_pr: bool = True


@dataclass(frozen=True)
class PlanResult:
    """The planner's full decision for one wakeup."""

    decision: Decision
    #: present when decision == "act"
    actions: list[Action] = field(default_factory=list)
    #: present when decision == "blocked"
    question: str = ""
    #: human-readable summary for the log + notify, any decision
    note: str = ""


@dataclass(frozen=True)
class PollResult:
    """Outcome of polling an in-flight devclaw ref."""

    terminal: bool
    #: pending | running | done | failed | cancelled | planning | ...
    status: str
    #: devclaw's full result/error blob, surfaced to the planner on terminal
    detail: str = ""

    @property
    def running(self) -> bool:
        return not self.terminal
