"""Live integration: the real HttpDevclawClient against a real devclaw server.

Skipped unless GOALCLAW_IT_DEVCLAW_URL points at a running devclaw (run it in
stub mode: DEVCLAW_ENGINE=stub DEVCLAW_TRANSPORT=http). This proves the one
thing the fakes can't — the fastmcp streamable-http client wiring + JSON parsing
against devclaw's actual tool surface.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from goalclaw.devclaw_client import HttpDevclawClient
from goalclaw.models import Action, Goal

URL = os.environ.get("GOALCLAW_IT_DEVCLAW_URL")
TOKEN = os.environ.get("GOALCLAW_IT_DEVCLAW_TOKEN", "")

pytestmark = pytest.mark.skipif(not URL, reason="set GOALCLAW_IT_DEVCLAW_URL to run")


@pytest.mark.asyncio
async def test_dispatch_and_poll_to_terminal():
    client = HttpDevclawClient(URL, TOKEN)
    ws = tempfile.mkdtemp(prefix="goalclaw-it-")
    goal = Goal(id="it", objective="x", cadence="1d", engine="devclaw", workspace_dir=ws, verify_cmd=None)
    action = Action(engine="devclaw", tool="implement_feature", goal="write a jyq cli", open_pr=False)

    ref = await client.dispatch(action, goal, notify_url="")
    assert ref.id
    assert ref.ref_kind == "task"

    # poll until terminal (stub finishes near-instantly; bound the wait)
    for _ in range(60):
        poll = await client.poll(ref)
        if poll.terminal:
            break
        await asyncio.sleep(0.5)
    assert poll.terminal, f"task never terminated; last status={poll.status}"
    assert poll.status in {"done", "failed"}
