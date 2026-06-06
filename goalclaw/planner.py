"""The plan step — the one cognition call, fired ONLY when the cheap check found
real work. Same split as devclaw's planner: Claude decides, Python validates the
JSON it emits. Decides the *next action* toward a goal — it does NOT decompose
code (that's devclaw's start_program). So it runs at the light tier (sonnet).
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Awaitable, Callable

from .models import Action, Goal, GoalStatus, PlanResult

CLAUDE_BIN = os.environ.get("GOALCLAW_CLAUDE_BIN", "claude")
PLANNER_TIMEOUT_MS = int(os.environ.get("GOALCLAW_PLANNER_TIMEOUT_MS", "90000"))

ClaudeCaller = Callable[[str], Awaitable[str]]

_VALID_TOOLS = {"start_program", "implement_feature", "fix_bug", "review_repository"}
_VALID_DECISIONS = {"act", "sleep", "blocked", "done"}


class PlannerError(Exception):
    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


SYSTEM_PROMPT = """You are GoalClaw's planner. You drive ONE durable goal forward
across many wakeups. On each wakeup you see the goal, its current state, recent
history, any new steering from Denys, and the result of the action that just
finished (if any). You decide the SINGLE next step.

You do NOT write code and you do NOT decompose work into a DAG — the code engine
(devclaw) does that. You choose ONE action and hand it to the engine, or you
decide there is nothing to do, or that you are blocked, or that the goal is done.

Rules:
- Exactly one action at a time. The engine runs it to completion (a PR you'll
  review) before you pick the next; keep momentum, don't fan out.
- Draw the next action from the goal's backlog and done_when. Prefer the
  smallest shippable next increment.
- If the finished action FAILED, decide: retry with a tighter instruction, pick
  a different next step, or block and ask Denys. Don't loop on the same failure.
- Honor new steering from Denys over the backlog — it is direction, not a
  suggestion.
- Use "start_program" for a goal-sized chunk devclaw should decompose itself;
  use "implement_feature" / "fix_bug" for a single bounded change; use
  "review_repository" for a read-only assessment.
- Block ONLY when you genuinely need a human decision (ambiguous requirement,
  external credential, product call). Asking is friction — prefer to act.
- Mark "done" only when done_when is satisfied by the history.

Respond with STRICT JSON ONLY — no prose, no markdown fences. Schema:

{
  "decision": "act" | "sleep" | "blocked" | "done",
  "note": "<one-line human summary of this decision, for the log + Telegram>",
  "actions": [            // present iff decision == "act"; exactly one element
    {
      "tool": "start_program" | "implement_feature" | "fix_bug" | "review_repository",
      "goal": "<concrete instruction for the engine>",
      "open_pr": true
    }
  ],
  "question": "<present iff decision == 'blocked' — what you need from Denys>"
}"""


def build_prompt(
    goal: Goal,
    status: GoalStatus,
    recent_log: str,
    steering: str,
    finished_detail: str,
) -> str:
    backlog = "\n".join(f"  - {b}" for b in goal.backlog) or "  (none listed)"
    parts = [
        SYSTEM_PROMPT,
        "\n## Goal",
        f"id: {goal.id}",
        f"objective: {goal.objective}",
        f"done_when: {goal.done_when or '(not specified)'}",
        f"engine: {goal.engine}  workspace_dir: {goal.workspace_dir}",
        f"verify_cmd: {goal.verify_cmd or '(none)'}",
        "backlog:",
        backlog,
        "\n## Current state",
        f"phase: {status.phase}",
        f"next (intended): {status.next or '(none)'}",
        "\n## Recent history (log)",
        recent_log or "(no events yet)",
    ]
    if finished_detail:
        parts += ["\n## The action that just finished (engine result)", finished_detail]
    if steering:
        parts += ["\n## NEW steering from Denys (honor this)", steering]
    parts.append("\nReturn the JSON now.")
    return "\n".join(parts)


def extract_json(text: str) -> str:
    trimmed = text.strip()
    if trimmed.startswith("{"):
        return trimmed
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", trimmed)
    if fence and fence.group(1):
        return fence.group(1)
    first, last = trimmed.find("{"), trimmed.rfind("}")
    if first >= 0 and last > first:
        return trimmed[first : last + 1]
    raise PlannerError("No JSON object found in planner response", text)


def validate(parsed: object) -> PlanResult:
    if not isinstance(parsed, dict):
        raise PlannerError("Plan must be a JSON object")
    decision = parsed.get("decision")
    if decision not in _VALID_DECISIONS:
        raise PlannerError(f"decision must be one of {_VALID_DECISIONS}, got {decision!r}")
    note = str(parsed.get("note", "")).strip()

    if decision == "blocked":
        question = str(parsed.get("question", "")).strip()
        if not question:
            raise PlannerError("blocked decision requires a non-empty 'question'")
        return PlanResult(decision="blocked", question=question, note=note or question)

    if decision in ("sleep", "done"):
        return PlanResult(decision=decision, note=note)

    # decision == "act"
    raw_actions = parsed.get("actions")
    if not isinstance(raw_actions, list) or len(raw_actions) != 1:
        raise PlannerError("act decision requires exactly one action")
    a = raw_actions[0]
    if not isinstance(a, dict):
        raise PlannerError("action must be an object")
    tool = a.get("tool")
    if tool not in _VALID_TOOLS:
        raise PlannerError(f"action.tool must be one of {_VALID_TOOLS}, got {tool!r}")
    g = str(a.get("goal", "")).strip()
    if not g:
        raise PlannerError("action.goal must be non-empty")
    action = Action(
        engine="devclaw",
        tool=tool,
        goal=g,
        verify_cmd=(str(a["verify_cmd"]).strip() if a.get("verify_cmd") else None),
        open_pr=bool(a.get("open_pr", True)),
    )
    return PlanResult(decision="act", actions=[action], note=note or g)


async def plan(
    goal: Goal,
    status: GoalStatus,
    recent_log: str,
    steering: str,
    finished_detail: str,
    *,
    claude_caller: ClaudeCaller,
) -> PlanResult:
    """Run the plan step. ``claude_caller`` is injected so tests stub the LLM."""
    prompt = build_prompt(goal, status, recent_log, steering, finished_detail)
    raw = await claude_caller(prompt)
    import json

    try:
        parsed = json.loads(extract_json(raw))
    except json.JSONDecodeError as exc:
        raise PlannerError(f"planner emitted invalid JSON: {exc}", raw) from exc
    return validate(parsed)


# ---- default cognition caller (mirrors devclaw.planner.call_claude) ----------


def _build_claude_argv(prompt: str, model: str | None) -> list[str]:
    argv = [CLAUDE_BIN, "--print", "--output-format=text"]
    if model:
        argv += ["--model", model]
    argv.append(prompt)
    return argv


async def call_claude(prompt: str, model: str | None = None) -> str:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    try:
        proc = await asyncio.create_subprocess_exec(
            *_build_claude_argv(prompt, model),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise PlannerError(f"Failed to spawn {CLAUDE_BIN}: {exc}") from exc
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=PLANNER_TIMEOUT_MS / 1000)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise PlannerError(f"claude --print timed out after {PLANNER_TIMEOUT_MS}ms")
    if proc.returncode != 0:
        raise PlannerError(
            f"claude --print exited {proc.returncode}. stderr:\n{err_b.decode('utf-8', 'replace')}"
        )
    return out_b.decode("utf-8", "replace")


def claude_with_model(model: str | None) -> ClaudeCaller:
    async def _caller(prompt: str) -> str:
        return await call_claude(prompt, model=model)

    return _caller
