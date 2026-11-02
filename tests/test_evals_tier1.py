"""The Tier 1 offline eval harness (ARCHITECTURE.md §12 Tier 1, Session 3).

The harness replays recorded sessions through Liber and reports metrics in the format
scripts/eval_gate.py compares against the committed baseline. Besides checking the numbers on
the real fixtures, each metric is shown to *move* when the thing it measures breaks: a metric
that can't fail is no gate.
"""

import json
import random
from pathlib import Path
from typing import Any

import pytest

from liber import pipeline
from liber.evals import tier1
from liber.evals.replay import load_sessions, replay
from tests.helpers import FIXTURES

SESSIONS = FIXTURES / "sessions"
METRICS = {
    "validity_rate",
    "round_trip_identity",
    "fallback_rate",
    "token_reduction",
    "prefix_stability",
    "render_determinism",
    "p95_overhead_ms",
}


def bodies(session_name: str) -> list[dict[str, Any]]:
    [session] = [s for s in load_sessions(SESSIONS) if s.name == session_name]
    return [json.loads(request.body) for request in replay(session)]


# --- Fixtures and replay ----------------------------------------------------------------------


def test_fixture_set_covers_what_tier1_needs() -> None:
    sessions = load_sessions(SESSIONS)
    blocks = [
        block
        for session in sessions
        for message in session.request["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
    ]

    assert len(sessions) >= 5
    assert any(block["type"] == "tool_use" for block in blocks)
    assert any(block["type"] == "thinking" for block in blocks)
    assert any(len(session.request["messages"]) >= 100 for session in sessions)


def test_replay_sends_one_request_per_user_turn() -> None:
    requests = bodies("chat_short")

    # 7 messages, user turns at 0, 2, 4, 6: each request is the history up to that turn.
    assert [len(r["messages"]) for r in requests] == [1, 3, 5, 7]
    assert all(r["messages"][-1]["role"] == "user" for r in requests)
    assert all(r["system"] == "You are a friendly assistant." for r in requests)


def test_replay_includes_retries_forks_and_rewrites() -> None:
    [session] = [s for s in load_sessions(SESSIONS) if s.name == "branches_and_rewrites"]
    requests = replay(session)
    labels = [r.label for r in requests]

    assert labels[:4] == ["turn 1", "turn 3", "turn 5", "turn 7"]
    assert labels[4:] == ["branch 1", "branch 2", "branch 3"]
    retry, fork, rewrite = (json.loads(r.body)["messages"] for r in requests[4:])
    assert len(retry) == 3
    assert fork[2]["content"] == "Now one about wind."
    assert rewrite[0]["content"].startswith("Summary so far")


def test_system_messages_end_a_request_only_after_a_user_turn() -> None:
    requests = bodies("system_messages")

    # Not the effort-only system message on its own, but the one that follows a user turn.
    assert [len(r["messages"]) for r in requests] == [2, 4, 5, 7]


def test_replay_moves_the_cache_marker_like_claude_code() -> None:
    for request in bodies("coding_tool_loops"):
        marked = [
            (i, j)
            for i, message in enumerate(request["messages"])
            if isinstance(message["content"], list)
            for j, block in enumerate(message["content"])
            if "cache_control" in block
        ]
        last = len(request["messages"]) - 1
        assert marked == [(last, len(request["messages"][last]["content"]) - 1)]


# --- Metrics on the real fixtures -----------------------------------------------------------


async def test_metrics_on_the_fixtures() -> None:
    result = await tier1.evaluate(SESSIONS)
    metrics = result.metrics

    assert set(metrics) == METRICS
    assert metrics["validity_rate"].value == 1.0
    assert metrics["round_trip_identity"].value == 1.0
    assert metrics["fallback_rate"].value == 0.0
    assert metrics["token_reduction"].value == 0.0  # no plans until Session 4
    assert metrics["prefix_stability"].value == 1.0  # placeholder until Session 4
    assert metrics["render_determinism"].value == 1.0
    lower_is_better = {"fallback_rate", "p95_overhead_ms"}
    assert all(m.direction == ("lower" if name in lower_is_better else "higher") for name, m in metrics.items())
    # The overhead tolerance is the budget left, so the gate fails once p95 goes over budget.
    # (Whether p95 is under budget is the request-path latency test's job, in test_pipeline.py:
    # asserting a timing in two places would only double the chance of a flaky failure.)
    p95 = metrics["p95_overhead_ms"]
    assert p95.value > 0
    assert p95.tolerance == pytest.approx(max(0.0, tier1.OVERHEAD_BUDGET_MS - p95.value))

    assert len(result.trace) == result.summary["requests"] >= 60
    first = result.trace[0]
    assert {"session", "label", "plan_version", "valid", "violations", "tokens_before", "tokens_after"} <= first.keys()


def test_cli_writes_the_gate_format_and_a_trace(tmp_path: Path) -> None:
    out = tmp_path / "results" / "latest.json"

    assert tier1.main(["--out", str(out), "--sessions", str(SESSIONS)]) == 0

    written = json.loads(out.read_text())
    assert set(written["metrics"]) == METRICS
    for metric in written["metrics"].values():
        assert {"value", "direction", "tolerance"} <= metric.keys()
    trace = [json.loads(line) for line in (tmp_path / "results" / "latest.trace.jsonl").read_text().splitlines()]
    assert len(trace) == written["summary"]["requests"]


def without_timings(results: dict[str, Any], trace: list[dict[str, Any]]) -> tuple[Any, Any]:
    results["metrics"].pop("p95_overhead_ms")
    for record in trace:
        record.pop("overhead_ms")
    return results, trace


def test_output_is_deterministic_apart_from_timings(tmp_path: Path) -> None:
    runs = []
    for name in ("a", "b"):
        out = tmp_path / name / "latest.json"
        tier1.main(["--out", str(out), "--sessions", str(SESSIONS)])
        trace = [json.loads(line) for line in out.with_name("latest.trace.jsonl").read_text().splitlines()]
        runs.append(without_timings(json.loads(out.read_text()), trace))

    assert runs[0] == runs[1]


# --- Each metric moves when what it measures breaks --------------------------------------------


async def test_render_determinism_catches_a_render_that_varies(monkeypatch: pytest.MonkeyPatch) -> None:
    real_render = tier1.render

    def jittery_render(parsed: Any, plan: Any) -> dict[str, Any]:
        rendered = real_render(parsed, plan)
        rendered["metadata"] = {"nonce": random.random()}  # different bytes on every render
        return rendered

    monkeypatch.setattr(tier1, "render", jittery_render)

    result = await tier1.evaluate(SESSIONS)

    assert result.metrics["render_determinism"].value == 0.0


async def test_validity_rate_catches_a_render_that_breaks_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    real_render = tier1.render

    def breaking_render(parsed: Any, plan: Any) -> dict[str, Any]:
        rendered = real_render(parsed, plan)
        rendered["messages"] = [{"role": "assistant", "content": "oops"}, *rendered["messages"]]
        return rendered

    monkeypatch.setattr(tier1, "render", breaking_render)

    result = await tier1.evaluate(SESSIONS)

    assert result.metrics["validity_rate"].value == 0.0
    assert any(record["violations"] for record in result.trace)


async def test_round_trip_identity_is_strict_about_key_order(monkeypatch: pytest.MonkeyPatch) -> None:
    real_render = tier1.render

    def reordering_render(parsed: Any, plan: Any) -> dict[str, Any]:
        rendered = real_render(parsed, plan)
        return dict(reversed(list(rendered.items())))  # equal as a dict, different bytes

    monkeypatch.setattr(tier1, "render", reordering_render)

    result = await tier1.evaluate(SESSIONS)

    assert result.metrics["round_trip_identity"].value == 0.0


async def test_fallback_rate_counts_requests_liber_stepped_aside_from(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_parse(request: Any) -> Any:
        raise RuntimeError("injected")

    monkeypatch.setattr(pipeline, "parse", failing_parse)

    result = await tier1.evaluate(SESSIONS)

    assert result.metrics["fallback_rate"].value == 1.0
    assert all(record["outcome"] == "fallback" for record in result.trace)
