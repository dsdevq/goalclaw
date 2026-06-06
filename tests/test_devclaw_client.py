"""poll-response parsing — read devclaw's camelCase wire shape.

devclaw's StateStore.to_dict emits camelCase (prUrl / resultJson). If goalclaw
reads snake_case the delivery evidence is silently dropped and the planner,
with no structured signal, misreads the raw blob (re-dispatch / false block).
These pin that the parser reads what devclaw actually sends.
"""

from __future__ import annotations

import json

from goalclaw.devclaw_client import parse_poll


def test_parse_poll_reads_camelcase_pr_and_gate():
    # exactly what devclaw get_status returns for a shipped, gate-passed task
    data = {
        "status": "done",
        "prUrl": "https://github.com/dsdevq/lifekit-dashboard/pull/21",
        "resultJson": json.dumps({"verify": {"ran": True, "passed": True, "exit_code": 0}}),
    }
    r = parse_poll(data)
    assert r.terminal is True
    assert r.pr_url == "https://github.com/dsdevq/lifekit-dashboard/pull/21"
    assert r.gate_passed is True


def test_parse_poll_snake_case_fallback():
    data = {
        "status": "done",
        "pr_url": "https://example/pull/1",
        "result_json": json.dumps({"verify": {"ran": True, "passed": False}}),
    }
    r = parse_poll(data)
    assert r.pr_url == "https://example/pull/1"
    assert r.gate_passed is False


def test_parse_poll_no_gate_is_none():
    r = parse_poll({"status": "done", "prUrl": None, "resultJson": json.dumps({})})
    assert r.gate_passed is None
    assert r.pr_url is None


def test_parse_poll_program_unwraps_status():
    r = parse_poll({"program": {"status": "running"}, "tasks": []}, "program")
    assert r.status == "running" and r.terminal is False
