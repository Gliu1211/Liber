"""Offline tests for the live check's decision logic (liber/evals/live.py). No network, no key."""

import json
from typing import Any

import pytest

from liber.evals.live import evaluate, summary_end


def _row(
    status: int = 200, outcome: str = "passthrough", plan: int | None = None, tokens: int = 1000, **extra: Any
) -> dict[str, Any]:
    row = {
        "status_code": status,
        "liber_outcome": outcome,
        "liber_error": None,
        "plan_version": plan,
        "edit_skipped": None,
        "input_tokens": tokens,
        "input_transformations": None,
    }
    row.update(extra)
    return row


def _good_rows() -> list[dict[str, Any]]:
    before = [_row(tokens=t) for t in (500, 900, 1400)]
    after = [
        _row(outcome="rendered", plan=1, tokens=600, input_transformations="[]"),
        _row(outcome="rendered", plan=1, tokens=700),
    ]
    return before + after


def _by_name(checks: list[Any]) -> dict[str, bool]:
    return {c.name: c.ok for c in checks}


def test_a_healthy_run_passes_every_check() -> None:
    checks = evaluate(_good_rows(), applied_at=3, needle_answer="It was BLUE HERON, due March 3.", worker_rows=4)
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_a_rejected_request_fails() -> None:
    rows = _good_rows()
    rows[3]["status_code"] = 400
    assert _by_name(evaluate(rows, 3, "blue heron march 3", 4))["no request rejected"] is False


def test_thinking_dropped_by_the_api_fails_adr_0012() -> None:
    rows = _good_rows()
    rows[3]["input_transformations"] = json.dumps([{"type": "thinking_dropped", "path": "messages.1.content.0"}])
    assert _by_name(evaluate(rows, 3, "blue heron march 3", 4))["API dropped no thinking (ADR 0012)"] is False


def test_other_transformation_types_are_not_counted_as_drops() -> None:
    rows = _good_rows()
    rows[3]["input_transformations"] = json.dumps([{"type": "something_new"}])
    assert _by_name(evaluate(rows, 3, "blue heron march 3", 4))["API dropped no thinking (ADR 0012)"] is True


def test_no_rendered_request_fails_compaction_and_the_drop_check() -> None:
    rows = [_row() for _ in range(5)]
    results = _by_name(evaluate(rows, 3, "blue heron march 3", 4))
    assert results["compaction sent"] is False
    assert results["API dropped no thinking (ADR 0012)"] is False  # can't pass vacuously


def test_a_bigger_context_after_compaction_fails() -> None:
    rows = _good_rows()
    rows[3]["input_tokens"] = 5000
    assert _by_name(evaluate(rows, 3, "blue heron march 3", 4))["context got smaller"] is False


def test_a_forgotten_needle_and_an_idle_worker_fail() -> None:
    results = _by_name(evaluate(_good_rows(), 3, "I don't recall.", 0))
    assert results["summary kept the planted fact"] is False
    assert results["worker wrote summaries"] is False


def test_summary_end_stops_before_the_next_turn_and_skips_tool_results() -> None:
    messages = [
        {"role": "user", "content": "turn 0"},
        {"role": "assistant", "content": []},
        {"role": "user", "content": "turn 1 asks for a tool"},
        {"role": "assistant", "content": []},
        {"role": "user", "content": [{"type": "tool_result"}]},
        {"role": "assistant", "content": []},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": []},
    ]
    assert summary_end(messages, [0, 2, 6, 8], through_turn=1) == 5
    with pytest.raises(ValueError):
        summary_end(messages, [0, 2, 4, 8], through_turn=1)  # 4 is a tool_result, not a turn start
