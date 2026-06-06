"""The engine seam — dispatch to devclaw over its HTTP MCP transport.

devclaw runs as a streamable-http FastMCP server (``devclaw-mcp:8000/mcp`` in
prod), bearer-token guarded. Every submission is async: the tool returns an id
immediately and the work runs in the background, so we *poll* the id on the next
heartbeat rather than block. That polling IS the cheap-idle check — a status
read costs zero tokens.

``DevclawClient`` is the Protocol the tick depends on; ``HttpDevclawClient`` is
the real implementation; tests inject a fake.
"""

from __future__ import annotations

import json
from typing import Protocol

from .models import Action, Goal, InFlight, PollResult

# devclaw task/program statuses that mean "finished, stop polling"
_TERMINAL = {"done", "failed", "cancelled", "error", "timeout"}


class DevclawError(RuntimeError):
    pass


class DevclawClient(Protocol):
    async def dispatch(self, action: Action, goal: Goal, notify_url: str) -> InFlight: ...
    async def poll(self, ref: InFlight) -> PollResult: ...


class HttpDevclawClient:
    def __init__(self, url: str, token: str = "") -> None:
        self._url = url
        self._token = token

    async def _call(self, tool: str, args: dict) -> dict:
        # Imported here so unit tests that use a fake client need no network deps.
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
        transport = StreamableHttpTransport(url=self._url, headers=headers)
        try:
            async with Client(transport) as client:
                result = await client.call_tool(tool, args)
        except Exception as exc:  # noqa: BLE001 — surface any transport/tool error uniformly
            raise DevclawError(f"devclaw {tool} call failed: {exc}") from exc
        return _parse_result(result, tool)

    async def dispatch(self, action: Action, goal: Goal, notify_url: str) -> InFlight:
        ws = goal.workspace_dir
        nu = notify_url or None
        if action.tool == "start_program":
            data = await self._call(
                "start_program", {"workspace_dir": ws, "goal": action.goal, "notify_url": nu}
            )
            return InFlight("devclaw", "start_program", _req(data, "program_id"), "program", action.goal)
        if action.tool in ("implement_feature", "fix_bug"):
            goal_key = "goal" if action.tool == "implement_feature" else "description"
            args = {
                "workspace_dir": ws,
                goal_key: action.goal,
                "notify_url": nu,
                "verify_cmd": action.verify_cmd or goal.verify_cmd,
                "open_pr": action.open_pr,
            }
            data = await self._call(action.tool, args)
            return InFlight("devclaw", action.tool, _req(data, "task_id"), "task", action.goal)
        if action.tool == "review_repository":
            data = await self._call(
                "review_repository", {"workspace_dir": ws, "focus": action.goal, "notify_url": nu}
            )
            return InFlight("devclaw", "review_repository", _req(data, "task_id"), "task", action.goal)
        raise DevclawError(f"unknown devclaw tool: {action.tool}")

    async def poll(self, ref: InFlight) -> PollResult:
        if ref.ref_kind == "program":
            data = await self._call("get_program", {"program_id": ref.id})
        else:
            data = await self._call("get_status", {"task_id": ref.id})
        return parse_poll(data, ref.ref_kind)


def parse_poll(data: dict, ref_kind: str = "task") -> PollResult:
    """Build a PollResult from a devclaw get_status / get_program response.

    devclaw's wire shape is **camelCase** (``StateStore.to_dict`` mirrors the
    original TS output: ``prUrl`` / ``resultJson``), so read those first and
    tolerate snake_case as a fallback. Without this the delivery evidence is
    silently dropped — ``pr_url``/``gate_passed`` come back None even on a
    shipped+gated task, and the planner, starved of structured signal, misreads
    the raw detail blob and re-dispatches or blocks. ``get_program`` wraps the
    row under ``"program"``.
    """
    row = data.get("program", data) if ref_kind == "program" else data
    status = str(row.get("status", "")).lower()
    pr_url = row.get("prUrl") or row.get("pr_url") or None
    return PollResult(
        terminal=status in _TERMINAL,
        status=status,
        detail=json.dumps(data)[:4000],
        pr_url=pr_url,
        gate_passed=_gate_passed(row),
    )


def _gate_passed(data: dict) -> "bool | None":
    """Pull the verify-gate verdict out of a devclaw task row, if it ran.
    devclaw emits ``resultJson`` (camelCase); tolerate ``result_json`` too."""
    rj = data.get("resultJson")
    if rj is None:
        rj = data.get("result_json")
    if isinstance(rj, str):
        try:
            rj = json.loads(rj)
        except json.JSONDecodeError:
            return None
    verify = rj.get("verify") if isinstance(rj, dict) else None
    if isinstance(verify, dict) and "passed" in verify:
        return bool(verify["passed"])
    return None


def _req(data: dict, key: str) -> str:
    val = data.get(key)
    if not val:
        raise DevclawError(f"devclaw response missing {key}: {data}")
    return str(val)


def _parse_result(result: object, tool: str) -> dict:
    """devclaw tools return a JSON string (TextContent). Tolerate a structured
    .data too, in case a future devclaw returns structured content."""
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise DevclawError(f"devclaw {tool} returned non-JSON: {text[:200]}") from exc
    raise DevclawError(f"devclaw {tool} returned no parseable content")
