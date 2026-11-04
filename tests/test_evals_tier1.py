"""The Tier 1 offline eval harness (ARCHITECTURE.md §12 Tier 1, Session 3).

The harness replays recorded sessions through Liber and reports metrics in the format
scripts/eval_gate.py compares against the committed baseline. Besides checking the numbers on
the real fixtures, each metric is shown to *move* when the thing it measures breaks: a metric
that can't fail is no gate.
"""

import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from liber import pipeline
from liber.evals import tier1
from liber.evals.replay import load_sessions, replay
from liber.plan.thinking import BINDING_BETA
from tests.helpers import FIXTURES

SESSIONS = FIXTURES / "sessions"
REPO_ROOT = Path(__file__).resolve().parents[1]
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
    # Branches are replayed turn by turn too, so continuing a fork is exercised, not just its end.
    assert labels[4:] == [
        "branch 1 turn 3",  # retry of turn 3
        "branch 2 turn 3",  # the edited prompt...
        "branch 2 turn 5",  # ...and the conversation continuing on the fork
        "branch 3 turn 1",  # the client's compaction summary...
        "branch 3 turn 3",  # ...and the next turn after it
    ]
    retry, fork, fork_continued, rewrite, _ = (json.loads(r.body)["messages"] for r in requests[4:])
    assert len(retry) == 3
    assert fork[2]["content"] == "Now one about wind." and len(fork) == 3
    assert len(fork_continued) == 5
    assert rewrite[0]["content"].startswith("Summary so far")


def test_system_messages_end_a_request_only_after_a_user_turn() -> None:
    requests = bodies("system_messages")

    # Not the effort-only system message on its own. And the user turn at 4 went out together
    # with the system message after it (5): the client never sent it alone.
    assert [len(r["messages"]) for r in requests] == [2, 5, 7]


def test_requests_are_compact_json_like_the_typescript_sdk() -> None:
    [session] = [s for s in load_sessions(SESSIONS) if s.name == "chat_short"]
    body = replay(session)[0].body

    assert b'", "' not in body and b'": ' not in body
    assert json.loads(body)["messages"][0]["content"] == "Hi! Can you help me plan a short trip?"


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
    # Standard plans (docs/decisions/0010), sent with preserved-thinking handling (ADR 0012):
    # real savings, and every minor compaction keeps the frozen prefix.
    assert metrics["token_reduction"].value > 0.2
    assert metrics["prefix_stability"].value == 1.0
    assert result.summary["minor_compactions_checked"] > 0
    outcomes = {(record["outcome"], record["plan_version"]) for record in result.trace}
    assert {("rendered", 1), ("rendered", 2)} <= outcomes
    # The beta goes with exactly the requests Liber edited (the fixtures use confirmed models).
    assert all(record["betas"] == [BINDING_BETA] for record in result.trace if record["outcome"] == "rendered")
    assert all(record["betas"] == [] for record in result.trace if record["outcome"] != "rendered")
    # ADR 0012: when a version takes effect, every earlier thinking block is stale and left out.
    first_with_version: dict[tuple[str, int], dict[str, Any]] = {}
    for record in result.trace:
        if record["outcome"] == "rendered":
            first_with_version.setdefault((record["session"], record["plan_version"]), record)
    assert first_with_version and all(record["thinking_sent"] == 0 for record in first_with_version.values())
    assert any(record["thinking_sent"] > 0 for record in result.trace if record["outcome"] == "rendered")
    lower_is_better = {"fallback_rate", "p95_overhead_ms"}
    assert all(m.direction == ("lower" if name in lower_is_better else "higher") for name, m in metrics.items())
    # The overhead tolerance is the budget left, so the gate fails once p95 goes over budget.
    # (Whether p95 is under budget is the request-path latency test's job, in test_pipeline.py:
    # asserting a timing in two places would only double the chance of a flaky failure.)
    p95 = metrics["p95_overhead_ms"]
    assert p95.value > 0
    # No max(0, ...): a baseline recorded over budget gets a negative tolerance, so the gate
    # still fails on anything over budget instead of accepting the over-budget value.
    assert p95.tolerance == pytest.approx(tier1.OVERHEAD_BUDGET_MS - p95.value)

    assert len(result.trace) == result.summary["requests"] >= 60
    first = result.trace[0]
    assert {
        "session",
        "label",
        "plan_version",
        "valid",
        "violations",
        "tokens_before",
        "tokens_after",
        "rendered_sha256",
    } <= first.keys()


def test_an_empty_run_is_an_error_not_a_perfect_score(tmp_path: Path) -> None:
    # No requests would make every metric look perfect; the run must fail instead.
    assert tier1.main(["--out", str(tmp_path / "latest.json"), "--sessions", str(tmp_path / "missing")]) != 0

    empty = tmp_path / "sessions"
    empty.mkdir()
    (empty / "only_assistant.json").write_text(
        json.dumps({"request": {"model": "m", "messages": [{"role": "assistant", "content": "hi"}]}})
    )
    assert tier1.main(["--out", str(tmp_path / "latest.json"), "--sessions", str(empty)]) != 0


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
    results["summary"].pop("max_overhead_ms")
    for record in trace:
        record.pop("overhead_ms")
    return results, trace


def test_output_is_deterministic_across_processes(tmp_path: Path) -> None:
    # Two separate `python -m` runs with different hash seeds: what a restart of Liber looks like.
    # Anything that depends on set or dict-of-hash ordering would show up in rendered_sha256.
    runs = []
    for seed in ("1", "2"):
        out = tmp_path / seed / "latest.json"
        env = {**os.environ, "PYTHONHASHSEED": seed}
        command = [sys.executable, "-m", "liber.evals.tier1", "--out", str(out), "--sessions", str(SESSIONS)]
        subprocess.run(command, check=True, capture_output=True, env=env, cwd=REPO_ROOT)
        trace = [json.loads(line) for line in out.with_name("latest.trace.jsonl").read_text().splitlines()]
        runs.append(without_timings(json.loads(out.read_text()), trace))

    assert runs[0] == runs[1]


# --- Each metric moves when what it measures breaks --------------------------------------------


async def test_render_determinism_catches_a_render_that_varies(monkeypatch: pytest.MonkeyPatch) -> None:
    real_render = tier1.render

    def jittery_render(parsed: Any, plan: Any, stale: int = 0) -> dict[str, Any]:
        rendered = real_render(parsed, plan, stale)
        rendered["metadata"] = {"nonce": random.random()}  # different bytes on every render
        return rendered

    monkeypatch.setattr(tier1, "render", jittery_render)

    result = await tier1.evaluate(SESSIONS)

    assert result.metrics["render_determinism"].value == 0.0


async def test_validity_rate_catches_a_render_that_breaks_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    # Validity is measured on what the request path itself rendered, so that's what is broken here.
    real_render = pipeline.render

    def breaking_render(parsed: Any, plan: Any, stale: int = 0) -> dict[str, Any]:
        rendered = real_render(parsed, plan, stale)
        rendered["messages"] = [{"role": "assistant", "content": "oops"}, *rendered["messages"]]
        return rendered

    monkeypatch.setattr(pipeline, "render", breaking_render)

    result = await tier1.evaluate(SESSIONS)

    assert result.metrics["validity_rate"].value == 0.0
    assert all(record["violations"] for record in result.trace)
    # Nothing invalid was sent, so nothing counts as saved.
    assert result.metrics["token_reduction"].value == 0.0


async def test_round_trip_identity_is_strict_about_key_order(monkeypatch: pytest.MonkeyPatch) -> None:
    real_render = tier1.render

    def reordering_render(parsed: Any, plan: Any, stale: int = 0) -> dict[str, Any]:
        rendered = real_render(parsed, plan, stale)
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
    # No plan was ever applied, so no minor compaction was measured: that fails, not passes.
    assert result.metrics["prefix_stability"].value == 0.0


async def test_prefix_stability_catches_a_compaction_that_rewrites_the_frozen_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_render = tier1.render

    def reordering_render(parsed: Any, plan: Any, stale: int = 0) -> dict[str, Any]:
        rendered = real_render(parsed, plan, stale)
        if sum(op.kind == "summarize" for op in plan.operations) == 2:
            # Puts the new frozen block before the old one: not an append (D5).
            first = rendered["messages"][0]
            content = [first["content"][1], first["content"][0], *first["content"][2:]]
            rendered["messages"] = [{**first, "content": content}, *rendered["messages"][1:]]
        return rendered

    monkeypatch.setattr(tier1, "render", reordering_render)

    result = await tier1.evaluate(SESSIONS)

    assert result.metrics["prefix_stability"].value == 0.0


def test_every_fixture_with_three_exchanges_gets_a_standard_plan() -> None:
    sessions = load_sessions(SESSIONS)
    plans = {session.name: tier1.standard_plan(session) for session in sessions}

    assert {name for name, plan in plans.items() if plan is not None} == {
        "branches_and_rewrites",
        "chat_short",
        "coding_tool_loops",
        "long_agentic_101",
        "media_and_redacted",
        "system_messages",
    }
    long_plan = plans["long_agentic_101"]
    assert long_plan is not None and [op.kind for op in long_plan.first] == ["summarize", "offload"]


async def test_token_reduction_counts_only_what_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    # A render that drops content but fails validation is never sent: no saving may be claimed.
    real_render = pipeline.render

    def lossy_invalid_render(parsed: Any, plan: Any, stale: int = 0) -> dict[str, Any]:
        rendered = real_render(parsed, plan, stale)
        rendered["messages"] = [*rendered["messages"][-1:], {"role": "user", "content": "again"}]
        return rendered

    monkeypatch.setattr(pipeline, "render", lossy_invalid_render)

    result = await tier1.evaluate(SESSIONS)

    assert result.metrics["token_reduction"].value == 0.0
    assert all(record["tokens_after"] == record["tokens_before"] for record in result.trace)
