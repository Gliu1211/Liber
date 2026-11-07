"""Tier 2 (liber/evals/tier2.py, tier2_report.py; ARCHITECTURE.md §12; Session 7 acceptance criteria).

Everything runs against fakes (hard rule 7). `FakeApi` behaves like the Messages API where it
matters here:
- a thinking block is bound to the exact history before it (its signature is a hash of that
  history), and with `prefix_mismatch_behavior: "error"` a mismatch is a 400 naming the block,
  like the real API (so Liber leaving stale thinking in would fail these tests);
- the "model" can only answer what its request contains: the dead end ruled out in its thinking
  ("pool 6/50, p99 4 ms") and the planted needle ("INC-4471").
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from liber.evals import tier2, tier2_report
from liber.evals.tier2 import ApiResult, ConditionRun, Settings
from tests.helpers import FakeModel

REASONING = "the database is ruled out: pool 6/50, p99 4 ms"
NEEDLE = "INC-4471"
TASKS = Path("evals/tier2_tasks")


def bound_to(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()[:16]


class FakeApi:
    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, Any], tuple[str, ...]]] = []

    async def send(self, request: dict[str, Any], betas: tuple[str, ...]) -> ApiResult:
        self.requests.append((request, betas))
        if request.get("system") == tier2.JUDGE_SYSTEM:
            return self.judge(request)
        messages = request["messages"]
        strict = (request.get("thinking") or {}).get("block_binding", {}).get("prefix_mismatch_behavior") == "error"
        for i, message in enumerate(messages):
            for j, block in enumerate(message["content"] if isinstance(message["content"], list) else []):
                if block.get("type") == "thinking" and block["signature"] != bound_to(messages[:i]) and strict:
                    error = (
                        f"messages.{i}.content.{j}: Invalid signature in thinking block: "
                        "bound to a different conversation"
                    )
                    return ApiResult(
                        400, {"type": "error", "error": {"type": "invalid_request_error", "message": error}}
                    )
        seen = json.dumps(messages)
        last = messages[-1]["content"]
        content: list[dict[str, Any]] = [
            {"type": "thinking", "thinking": f"Considered it: {REASONING}.", "signature": bound_to(messages)}
        ]
        stop = "end_turn"
        if isinstance(last, str) and "Call read_logs" in last:
            content.append({"type": "tool_use", "id": "toolu_1", "name": "read_logs", "input": {}})
            stop = "tool_use"
        elif isinstance(last, str) and last == tier2.COMPACT_PROMPT:
            content.append(
                {
                    "type": "text",
                    "text": "Summary: the checkout 500s come from an expired payments certificate; it was rotated.",
                }
            )
        elif isinstance(last, str) and "rule out the database" in last:
            answer = f"ruled out: {REASONING}" if REASONING in seen else "not considered; it was the TLS certificate"
            content.append({"type": "text", "text": answer})
        elif isinstance(last, str) and "incident ticket" in last and "on call" in last:
            content.append({"type": "text", "text": f"{NEEDLE}, Priya Raman" if NEEDLE in seen else "I don't know"})
        else:
            content.append({"type": "text", "text": "Expired payments certificate." + " Details follow." * 40})
        usage = {
            "input_tokens": len(seen) // 4,
            "output_tokens": 20,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        return ApiResult(
            200, {"type": "message", "role": "assistant", "content": content, "stop_reason": stop, "usage": usage}
        )

    def judge(self, request: dict[str, Any]) -> ApiResult:
        text = request["messages"][0]["content"].removeprefix("<grading>\n").removesuffix("\n</grading>")
        grading = json.loads(text)
        if "states this fact" in grading["rubric"]:
            verdict = NEEDLE in grading["answer"]
        else:
            verdict = grading["answer"].startswith("ruled out")
        body = {
            "content": [{"type": "text", "text": json.dumps({"pass": verdict, "reason": "fake judge"})}],
            "stop_reason": "end_turn",
        }
        return ApiResult(200, body)


def worker_model() -> FakeModel:
    """A faithful summarizer: keeps the needle; notes repeat the reasoning when they can read it."""
    model = FakeModel()

    def reply(call: dict[str, Any]) -> dict[str, Any]:
        if "notes" in call["schema"]["properties"]:
            return {"notes": [REASONING] if REASONING in call["prompt"] else []}
        kept = f" Ticket {NEEDLE}, on call Priya Raman." if NEEDLE in call["prompt"] else ""
        return {"topic": "checkout 500s", "summary": "Checkout 500s traced to an expired payments certificate." + kept}

    model.reply = reply
    return model


SETTINGS = Settings(model="claude-sonnet-5-5", judge_model="claude-opus-5-5", repeats=2)


@pytest.fixture
def dead_end() -> tier2.Task:
    [task] = [t for t in tier2.load_tasks(TASKS) if t.name == "dead_end_debugging"]
    return task


async def test_the_four_conditions_end_to_end_against_fakes(dead_end: tier2.Task) -> None:
    api = FakeApi()

    runs = await tier2.run_eval(api, worker_model(), [dead_end], SETTINGS, progress=lambda _: None)

    by = {(r.condition, r.repeat): r for r in runs}
    assert len(runs) == 8  # 4 conditions x 2 repeats
    for repeat in (1, 2):
        a, b, c, d = (by[(cond, repeat)] for cond in "ABCD")
        # A keeps the thinking, B drops it, C gets it back as notes, D's compaction lost both.
        assert (a.success, b.success, c.success, d.success) == (True, False, True, False)
        assert (a.needle_kept, b.needle_kept, c.needle_kept, d.needle_kept) == (True, True, True, False)
        for run in (a, b, c, d):
            assert run.binding_errors == [] and run.errors == []
        # Liber sent the compaction on every B/C request (it took effect at the probe).
        assert b.liber_outcomes == c.liber_outcomes == ["rendered", "rendered"]
        assert b.usage.input_tokens < a.usage.input_tokens  # compaction made requests smaller
        assert d.overhead.input_tokens > 0  # D's own compaction call is counted
        assert all(run.cost is not None and run.cost > 0 for run in (a, b, c, d))
    # Every request asked for strict binding, with the beta.
    sent = [(r, betas) for r, betas in api.requests if r.get("system") != tier2.JUDGE_SYSTEM]
    assert all(r["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "error"} for r, _ in sent)
    assert all(betas == (tier2.BINDING_BETA,) for _, betas in sent)


async def test_b_and_c_use_identical_summaries_apart_from_the_notes(dead_end: tier2.Task) -> None:
    api = FakeApi()
    await tier2.run_eval(api, worker_model(), [dead_end], Settings("claude-sonnet-5-5", "j", repeats=1), lambda _: None)

    frozen = [
        block["text"]
        for request, _ in api.requests
        if request.get("system") != tier2.JUDGE_SYSTEM
        for message in request["messages"][:1]
        if isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "text" and block["text"].startswith("<liber:summary")
    ]
    b_block, c_block = frozen[0], frozen[2]  # each condition sends two requests
    assert REASONING not in b_block and REASONING in c_block
    assert (
        c_block.replace(f"- {REASONING}\n", "").replace(
            "Earlier decisions and why (from the assistant's reasoning at the time):\n", ""
        )
        == b_block
    )


async def test_a_binding_break_is_reported_with_its_path(dead_end: tier2.Task) -> None:
    run = ConditionRun("t", "B", 1)
    error = "messages.3.content.0: Invalid signature in thinking block: bound to a different conversation"

    async def rejecting(messages: list[dict[str, Any]]) -> ApiResult:
        return ApiResult(400, {"type": "error", "error": {"type": "invalid_request_error", "message": error}})

    answer = await tier2.turn(rejecting, [], "hello", dead_end, run)

    assert answer is None
    assert run.binding_errors == [f"messages.3.content.0: {error}"]
    assert tier2.binding_error(ApiResult(400, {"error": {"message": "max_tokens: too large"}})) is None
    assert tier2.binding_error(ApiResult(500, {"error": {"message": "thinking signature"}})) is None


def test_it_refuses_to_run_without_live(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-used")

    assert tier2.main([]) == 2
    assert "--live" in capsys.readouterr().out
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert tier2.main(["--live"]) == 2  # and not without a key either
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_the_task_fixtures_load_and_one_is_reasoning_dependent() -> None:
    tasks = tier2.load_tasks(TASKS)

    assert any(task.reasoning_dependent for task in tasks)
    for task in tasks:
        assert task.needle_fact and task.probe_rubric and set(task.tool_outputs) == {t["name"] for t in task.tools}


# --- The report on a fixed dataset --------------------------------------------------------------------


def run(task: str, condition: str, repeat: int, success: bool, tokens: int, needle: bool = True) -> dict[str, Any]:
    usage = {
        "input_tokens": tokens // 2,
        "cache_read": tokens // 2,
        "cache_write": 0,
        "output_tokens": 50,
        "thinking_dropped": 0,
    }
    return {
        "task": task,
        "condition": condition,
        "repeat": repeat,
        "success": success,
        "needle_kept": needle,
        "usage": usage,
        "overhead": {k: 0 for k in usage},
        "cost": tokens / 1_000_000,
        "binding_errors": [],
        "errors": [],
        "liber_outcomes": [],
        "answers": {},
        "judge_reasons": {},
    }


# t1: success A 3/3, B 1/3, C 2/3, D 0/3; tokens A 1000, B 600, C 650, D 300.
# t2: success A 2/2, B 2/2, C 2/2, D 1/2; tokens A 2000, B 1000, C 1000, D 500.
FIXED = (
    [run("t1", "A", i, True, 1000) for i in (1, 2, 3)]
    + [run("t1", "B", i, ok, 600) for i, ok in ((1, False), (2, False), (3, True))]
    + [run("t1", "C", i, ok, 650) for i, ok in ((1, True), (2, False), (3, True))]
    + [run("t1", "D", i, False, 300, needle=False) for i in (1, 2, 3)]
    + [run("t2", cond, i, True, tokens) for cond, tokens in (("A", 2000), ("B", 1000), ("C", 1000)) for i in (1, 2)]
    + [run("t2", "D", 1, True, 500), run("t2", "D", 2, False, 500)]
)


def test_the_report_computes_the_three_comparisons() -> None:
    per_task = {task: tier2_report.compare(stats) for task, stats in tier2_report.summarize(FIXED).items()}
    t1 = {c.name: c for c in per_task["t1"]}
    t2 = {c.name: c for c in per_task["t2"]}
    everything = {c.name: c for c in tier2_report.overall(per_task)}

    assert t1["A vs B"].success_delta == pytest.approx(2 / 3)
    assert t1["A vs B"].input_change == pytest.approx(1000 / 600 - 1)
    assert t1["C vs B"].success_delta == pytest.approx(1 / 3)
    assert t1["B vs D"].success_delta == pytest.approx(1 / 3)
    assert t1["C vs D"].success_delta == pytest.approx(2 / 3)
    assert t1["C vs D"].input_change == pytest.approx(650 / 300 - 1)
    assert t2["A vs B"].success_delta == pytest.approx(0.0)
    assert t2["B vs D"].success_delta == pytest.approx(0.5)
    assert t2["B vs D"].input_change == pytest.approx(1.0)
    # Averaged over tasks.
    assert everything["A vs B"].success_delta == pytest.approx((2 / 3 + 0) / 2)
    assert everything["B vs D"].success_delta == pytest.approx((1 / 3 + 0.5) / 2)
    assert everything["C vs B"].input_change == pytest.approx((650 / 600 - 1 + 0) / 2)


def test_the_report_shows_mean_and_spread() -> None:
    stats = tier2_report.summarize(FIXED)["t1"]

    assert stats["B"].success.mean == pytest.approx(1 / 3)
    assert (stats["B"].success.low, stats["B"].success.high) == (0.0, 1.0)
    assert stats["B"].success.stdev == pytest.approx(0.57735, rel=1e-4)
    assert stats["D"].needle.mean == 0.0
    assert stats["A"].input_tokens.mean == 1000 and stats["A"].input_tokens.stdev == 0.0


def test_history_files_and_trends(tmp_path: Path) -> None:
    meta = {"model": "m", "judge_model": "j", "summary_model": "s", "repeats": 3}
    first_day = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
    older = [r for r in FIXED if r["task"] == "t2"]

    tier2_report.write_history(tmp_path, older, meta, first_day)
    json_path, report_path = tier2_report.write_history(tmp_path, FIXED, meta, first_day.replace(hour=11))
    points = tier2_report.trends(tmp_path)
    text = report_path.read_text()

    assert json_path.name == "2026-11-07-110000.json"  # the day's second run keeps the first
    assert json.loads(json_path.read_text())["runs"] == FIXED
    assert [date for date, _ in points] == ["2026-11-07", "2026-11-07-110000"]
    assert points[0][1]["D"] == (0.5, 500.0)
    assert "| A vs B | what dropping thinking costs | +33% | +83% |" in text
    assert "## Trends" in text and "## t1" in text and "## t2" in text
    assert "0% (0% to 100%, sd 58%)" not in text  # B's success on t1 reads as a share
    assert "33% (0% to 100%, sd 58%)" in text


async def test_the_fake_catches_liber_leaving_stale_thinking_in(
    dead_end: tier2.Task, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding check above is real: break ADR 0012's stripping and B's requests fail by path."""
    from liber import pipeline
    from liber.model.anthropic import render

    monkeypatch.setattr(pipeline, "render", lambda parsed, plan, stale_thinking_before=0: render(parsed, plan, 0))
    api = FakeApi()

    runs = await tier2.run_eval(
        api, worker_model(), [dead_end], Settings(SETTINGS.model, "j", repeats=1), lambda _: None
    )

    b = next(r for r in runs if r.condition == "B")
    assert b.binding_errors and b.binding_errors[0].startswith("messages.")
    assert b.success is None  # nothing to grade: the request was rejected


async def test_a_compaction_liber_did_not_send_is_an_error_not_a_silent_a(dead_end: tier2.Task) -> None:
    unknown_model = Settings("some-other-model", "j", repeats=1)  # Liber won't edit thinking it can't confirm

    runs = await tier2.run_eval(FakeApi(), worker_model(), [dead_end], unknown_model, lambda _: None)

    b = next(r for r in runs if r.condition == "B")
    assert b.errors and b.errors[0].startswith("Liber didn't send the compaction: passthrough")
