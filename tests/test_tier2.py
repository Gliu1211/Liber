"""Tier 2 (liber/evals/tier2.py, tier2_api.py, tier2_report.py; ARCHITECTURE.md §12; Session 7).

Everything runs against fakes (hard rule 7). These tests check the plumbing, not Liber's quality:
`FakeApi`'s answers are rigged so each condition's outcome shows whether the right context reached
the model. It behaves like the Messages API where the plumbing can go wrong:
- a thinking block is bound to the exact history before it (in this fake, its signature is a hash
  of that history), and with `prefix_mismatch_behavior: "error"` a mismatch is a 400 naming the
  block, so Liber leaving stale thinking in fails these tests;
- thinking comes back with text only when `display: "summarized"` is asked for;
- the "model" can only answer what its request contains: the dead end ruled out in its thinking
  ("pool 6/50, p99 4 ms") and the planted needle ("INC-4471").
"""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from liber.evals import tier2, tier2_api, tier2_report
from liber.evals.tier2 import ConditionRun, Settings
from liber.evals.tier2_api import ApiResult, HttpMessagesApi, Usage
from tests.helpers import FakeModel

REASONING = "the database is ruled out: pool 6/50, p99 4 ms"
NEEDLE = "INC-4471"
CONFIG_NEEDLE = "CHG-2291"
SMALL_NEEDLE = "OPS-8812"
CODE_NEEDLE = "DEV-318"
# code_reconstruction_reread: what the fake "model" saves, and how its probe is recognised.
EDITED = "# edited: empty-input check, docstring"
PARSE_ANSWER = f"def parse(line):\n    {EDITED}\n    ..."
REREAD_PROBE = "in etl/parser.py exactly as it stands now"
# code_reconstruction: the fake "model" prints this as its final parse() (setup turn 2's reply).
FINAL_CODE = 'def parse(line):\n    """Parse one line of the export format."""\n    # final version'


def write_call(number: int) -> dict[str, Any]:
    """The fake model saves etl/parser.py with all the task's edits (what workspace_checks expects)."""
    source = json.loads((TASKS / "code_reconstruction_reread.json").read_text())["files"]["etl/parser.py"]
    edited = source.replace("def load(", "def load_records(").replace(
        "def parse(line):\n", f'def parse(line):\n    """Parse one line of the export format."""\n    {EDITED}\n'
    )
    return {
        "type": "tool_use",
        "id": f"toolu_{number}",
        "name": "write_file",
        "input": {"path": "etl/parser.py", "content": edited},
    }


TASKS = Path(__file__).parent.parent / "evals" / "tier2_tasks"
SETTINGS = Settings(model="claude-sonnet-5-5", judge_model="claude-opus-5-5", repeats=2)


def bound_to(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()[:16]


def message(content: list[dict[str, Any]], stop: str, seen: str) -> ApiResult:
    usage = {"input_tokens": len(seen) // 4, "output_tokens": 20}
    return ApiResult(
        200, {"type": "message", "role": "assistant", "content": content, "stop_reason": stop, "usage": usage}
    )


class FakeApi:
    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, Any], tuple[str, ...]]] = []

    async def send(self, request: dict[str, Any], betas: tuple[str, ...]) -> ApiResult:
        self.requests.append((request, betas))
        if (request.get("thinking") or {}).get("type") == "disabled":
            # Like the real API on Opus 5.5 / Sonnet 5.5: thinking can't be turned off. (The first
            # live run lost every verdict to this; the fake now fails the same way, offline.)
            return ApiResult(400, {"error": {"message": '"thinking.type.disabled" is not supported for this model.'}})
        if request.get("system") == tier2_api.JUDGE_SYSTEM:
            return self.judge(request)
        messages = request["messages"]
        thinking = request.get("thinking") or {}
        if thinking.get("block_binding", {}).get("prefix_mismatch_behavior") == "error":
            for i, earlier in enumerate(messages):
                for j, block in enumerate(earlier["content"] if isinstance(earlier["content"], list) else []):
                    if block.get("type") == "thinking" and block["signature"] != bound_to(messages[:i]):
                        error = (
                            f"messages.{i}.content.{j}: Invalid signature in thinking block: "
                            "bound to a different conversation"
                        )
                        return ApiResult(
                            400, {"type": "error", "error": {"type": "invalid_request_error", "message": error}}
                        )
        seen = json.dumps(messages)
        text = f"Considered it: {REASONING}." if thinking.get("display") == "summarized" else ""
        content: list[dict[str, Any]] = [{"type": "thinking", "thinking": text, "signature": bound_to(messages)}]
        last = messages[-1]["content"]
        if not isinstance(last, str):  # tool results came back
            asked = next(m["content"] for m in reversed(messages) if isinstance(m["content"], str))
            wrote = "Wrote " in json.dumps(last)
            if "save" in asked.lower() and not wrote:  # code_reconstruction_reread: save after reading
                return message([*content, write_call(len(messages))], "tool_use", seen)
            if REREAD_PROBE in asked:  # its probe, answered after reading the file
                answer = PARSE_ANSWER if EDITED in seen else "I can't tell."
                return message([*content, {"type": "text", "text": answer}], "end_turn", seen)
            content.append({"type": "text", "text": "Expired payments certificate." + " Details follow." * 40})
            return message(content, "end_turn", seen)
        tool = next((t["name"] for t in request.get("tools", []) if f"Call {t['name']}" in last), None)
        if tool is not None:
            arguments = {"path": "etl/parser.py"} if tool == "read_file" else {}
            content.append({"type": "tool_use", "id": f"toolu_{len(messages)}", "name": tool, "input": arguments})
            return message(content, "tool_use", seen)
        if "save" in last.lower() and any(t["name"] == "write_file" for t in request.get("tools", [])):
            return message([*content, write_call(len(messages))], "tool_use", seen)
        if REREAD_PROBE in last:  # answer from context, or reread the file
            if EDITED in seen:
                return message([*content, {"type": "text", "text": PARSE_ANSWER}], "end_turn", seen)
            read = {
                "type": "tool_use",
                "id": f"toolu_{len(messages)}",
                "name": "read_file",
                "input": {"path": "etl/parser.py"},
            }
            return message([*content, read], "tool_use", seen)
        if last == tier2.COMPACT_PROMPT:
            answer = "Summary: the checkout 500s come from an expired payments certificate; it was rotated."
        elif "rule out the database" in last:
            answer = f"ruled out: {REASONING}" if REASONING in seen else "not considered; it was the TLS certificate"
        elif "incident ticket" in last and "on call" in last:
            answer = f"{NEEDLE}, Priya Raman" if NEEDLE in seen else "I don't know"
        elif "retry.backoff_multiplier" in last:  # undiscussed_config_detail's probe
            answer = "1.7" if "backoff_multiplier = 1.7" in seen else "I don't know"
        elif "change ticket" in last:  # its needle
            answer = f"{CONFIG_NEEDLE}, Tomasz Wierzbicki" if CONFIG_NEEDLE in seen else "I don't know"
        elif "stale_while_revalidate_s" in last:  # obscure_setting_in_big_config's probe
            answer = "35" if "stale_while_revalidate_s = 35" in seen else "I don't know"
        elif "Reply with the full updated parse() only" in last:  # code_reconstruction's setup
            version = FINAL_CODE if "docstring" in last else "def parse(line):\n    # first edit\n    ..."
            answer = f"```python\n{version}\n```"
        elif "exactly as it stands after all our edits" in last:  # code_reconstruction's probe
            # `seen` is JSON (newlines escaped), so look for the code's one-line marker.
            answer = FINAL_CODE if "# final version" in seen else "I can't reproduce it exactly from memory."
        elif "who is the reviewer" in last:  # code_reconstruction(_reread)'s needle
            answer = f"{CODE_NEEDLE}, Ines Duarte" if CODE_NEEDLE in seen else "I don't know"
        elif "ticket did I give you" in last:  # its needle
            answer = f"{SMALL_NEEDLE}, Lena Brandt" if SMALL_NEEDLE in seen else "I don't know"
        else:
            answer = "Done." + " Details follow." * 40
        content.append({"type": "text", "text": answer})
        return message(content, "end_turn", seen)

    def judge(self, request: dict[str, Any]) -> ApiResult:
        text = request["messages"][0]["content"].removeprefix("<grading>\n").removesuffix("\n</grading>")
        grading = json.loads(text)
        if "states this fact" in grading["rubric"]:
            tags = (NEEDLE, CONFIG_NEEDLE, SMALL_NEEDLE, CODE_NEEDLE)
            verdict = any(tag in grading["answer"] for tag in tags if tag in grading["rubric"])
        elif "Parse one line of the export format" in grading["rubric"]:
            verdict = EDITED in grading["answer"] or FINAL_CODE in grading["answer"]
        elif "the value 1.7" in grading["rubric"]:
            verdict = "1.7" in grading["answer"]
        elif "gives 35 for [cache.suggest]" in grading["rubric"]:
            verdict = "35" in grading["answer"]
        else:
            verdict = grading["answer"].startswith("ruled out")
        reply = json.dumps({"pass": verdict, "reason": "fake judge"})
        return ApiResult(200, {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn"})


def worker_model() -> FakeModel:
    """A faithful summarizer: keeps the needle; notes repeat the reasoning when they can read it."""
    model = FakeModel()

    def reply(call: dict[str, Any]) -> dict[str, Any]:
        if "notes" in call["schema"]["properties"]:
            return {"notes": [REASONING] if REASONING in call["prompt"] else []}
        tags = (NEEDLE, CONFIG_NEEDLE, SMALL_NEEDLE, CODE_NEEDLE)
        kept = "".join(f" Ticket {tag}." for tag in tags if tag in call["prompt"])
        return {"topic": "checkout 500s", "summary": "Checkout 500s traced to an expired payments certificate." + kept}

    model.reply = reply
    return model


def task_named(name: str) -> tier2.Task:
    [task] = [t for t in tier2.load_tasks(TASKS) if t.name == name]
    return task


@pytest.fixture
def dead_end() -> tier2.Task:
    return task_named("dead_end_debugging")


def quiet(_: str) -> None:
    pass


async def test_the_four_conditions_end_to_end_against_fakes(dead_end: tier2.Task) -> None:
    api = FakeApi()

    runs = await tier2.run_eval(api, worker_model(), [dead_end], SETTINGS, progress=quiet)

    by = {(r.condition, r.repeat): r for r in runs}
    assert len(runs) == 8  # 4 conditions x 2 repeats
    for repeat in (1, 2):
        a, b, c, d = (by[(cond, repeat)] for cond in "ABCD")
        # A keeps the thinking, B drops it, C gets it back as notes, D's compaction lost both.
        assert (a.success, b.success, c.success, d.success) == (True, False, True, False)
        assert (a.needle_kept, b.needle_kept, c.needle_kept, d.needle_kept) == (True, True, True, False)
        for run in (a, b, c, d):
            assert run.binding_errors == [] and run.errors == []
            assert run.reasoning_dependent and run.thinking_with_text > 0
        # Liber sent the compaction on every B/C request (it took effect at the probe).
        assert b.liber_outcomes == c.liber_outcomes == ["rendered", "rendered"]
        assert b.summary_has_evidence is False  # B's result isn't the summary's doing
        assert b.usage.input_tokens < a.usage.input_tokens  # compaction made requests smaller
        assert d.overhead.input_tokens > 0  # D's own compaction call is counted
        assert all(run.cost is not None and run.cost > 0 for run in (a, b, c, d))
    # Every request asked for strict binding, with the beta, and for the thinking text.
    sent = [(r, betas) for r, betas in api.requests if r.get("system") != tier2_api.JUDGE_SYSTEM]
    assert all(r["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "error"} for r, _ in sent)
    assert all(r["thinking"]["display"] == "summarized" for r, _ in sent)
    assert all(betas == (tier2_api.BINDING_BETA,) for _, betas in sent)


async def test_the_factual_task_runs_end_to_end_too() -> None:
    runs = await tier2.run_eval(FakeApi(), worker_model(), [task_named("migration_facts")], SETTINGS, quiet)

    assert len(runs) == 8 and all(r.errors == [] and r.binding_errors == [] for r in runs)
    assert not any(r.reasoning_dependent for r in runs)


async def test_b_and_c_use_identical_summaries_apart_from_the_notes(dead_end: tier2.Task) -> None:
    runs = await tier2.run_eval(FakeApi(), worker_model(), [dead_end], Settings(SETTINGS.model, "j", repeats=1), quiet)

    b = next(r for r in runs if r.condition == "B")
    c = next(r for r in runs if r.condition == "C")
    [b_block], [c_block] = b.frozen_blocks, c.frozen_blocks
    heading = "Earlier decisions and why (from the assistant's reasoning at the time):"
    assert REASONING not in b_block and heading not in b_block
    c_lines = c_block.splitlines()
    notes_at = c_lines.index(heading)
    assert c_lines[:notes_at] + c_lines[-1:] == b_block.splitlines()  # only the notes section differs
    assert all(REASONING in line for line in c_lines[notes_at + 1 : -1])


async def test_the_fake_catches_liber_leaving_stale_thinking_in(
    dead_end: tier2.Task, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding check is real: break ADR 0012's stripping and B fails, reported by path."""
    from liber import pipeline
    from liber.model.anthropic import render

    monkeypatch.setattr(pipeline, "render", lambda parsed, plan, stale_thinking_before=0: render(parsed, plan, 0))

    runs = await tier2.run_eval(FakeApi(), worker_model(), [dead_end], Settings(SETTINGS.model, "j", repeats=1), quiet)

    b = next(r for r in runs if r.condition == "B")
    assert b.binding_errors and b.binding_errors[0].startswith("messages.")
    assert b.success is False and b.needle_kept is False  # a broken request is a failed answer


async def test_a_compaction_liber_did_not_send_is_an_error_not_a_silent_a(dead_end: tier2.Task) -> None:
    unknown_model = Settings("some-other-model", "j", repeats=1)  # Liber won't edit thinking it can't confirm

    runs = await tier2.run_eval(FakeApi(), worker_model(), [dead_end], unknown_model, quiet)

    b = next(r for r in runs if r.condition == "B")
    assert b.errors and b.errors[0].startswith("Liber didn't send the compaction: passthrough")


async def test_without_thinking_text_the_reasoning_task_says_c_cannot_differ(
    dead_end: tier2.Task, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tier2.base_request

    def no_display(task: tier2.Task, model: str) -> dict[str, Any]:
        return {**original(task, model), "thinking": {"type": "adaptive"}}  # thinking text omitted

    monkeypatch.setattr(tier2, "base_request", no_display)

    runs = await tier2.run_eval(FakeApi(), worker_model(), [dead_end], Settings(SETTINGS.model, "j", repeats=1), quiet)

    assert all(r.thinking_with_text == 0 for r in runs)
    assert all(any("no thinking text" in e for e in r.errors) for r in runs)


async def test_a_failed_repeat_is_recorded_and_the_rest_is_kept(dead_end: tier2.Task) -> None:
    class FailsSecondRepeat(FakeApi):
        def __init__(self) -> None:
            super().__init__()
            self.setups = 0

        async def send(self, request: dict[str, Any], betas: tuple[str, ...]) -> ApiResult:
            if request["messages"] == [{"role": "user", "content": dead_end.turns[0]}]:  # a repeat's first turn
                self.setups += 1
                if self.setups == 2:
                    return ApiResult(529, {"error": {"message": "overloaded"}})
            return await super().send(request, betas)

    saved: list[int] = []
    runs = await tier2.run_eval(
        FailsSecondRepeat(), worker_model(), [dead_end], SETTINGS, quiet, save=lambda so_far: saved.append(len(so_far))
    )

    assert saved == [4, 8]  # saved after each repeat
    first, second = runs[:4], runs[4:]
    assert all(r.success is not None for r in first)
    assert all(r.errors and r.errors[0].startswith("repeat failed") for r in second)


# --- Turns, binding errors, the judge -------------------------------------------------------------------


async def test_a_binding_break_is_reported_with_its_path(dead_end: tier2.Task) -> None:
    run = ConditionRun("t", "B", 1)
    error = "messages.3.content.0: Invalid signature in thinking block: bound to a different conversation"

    async def rejecting(messages: list[dict[str, Any]]) -> ApiResult:
        return ApiResult(400, {"type": "error", "error": {"type": "invalid_request_error", "message": error}})

    answer = await tier2.turn(rejecting, [], "hello", dead_end, run)

    assert answer is None
    assert run.binding_errors == [f"messages.3.content.0: {error}"]
    assert tier2_api.binding_error(ApiResult(400, {"error": {"message": "max_tokens: too large"}})) is None
    assert tier2_api.binding_error(ApiResult(500, {"error": {"message": "thinking signature"}})) is None


async def test_a_cut_off_reply_or_endless_tool_calls_is_an_error(dead_end: tier2.Task) -> None:
    async def cut_off(messages: list[dict[str, Any]]) -> ApiResult:
        return ApiResult(200, {"content": [{"type": "text", "text": "partial"}], "stop_reason": "max_tokens"})

    async def always_tools(messages: list[dict[str, Any]]) -> ApiResult:
        call = {"type": "tool_use", "id": f"t{len(messages)}", "name": "read_logs", "input": {}}
        return ApiResult(200, {"content": [call], "stop_reason": "tool_use"})

    first, second = ConditionRun("t", "A", 1), ConditionRun("t", "A", 1)

    assert await tier2.turn(cut_off, [], "q", dead_end, first) is None
    assert await tier2.turn(always_tools, [], "q", dead_end, second) is None
    assert first.errors == ["a reply was cut off at max_tokens"]
    assert second.errors == [f"the model was still calling tools after {tier2.MAX_TOOL_ROUNDS} rounds"]


def judged(text: str, stop: str = "end_turn") -> ApiResult:
    return ApiResult(200, {"content": [{"type": "text", "text": text}], "stop_reason": stop})


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (judged('{"pass": true, "reason": "ok"}'), True),
        (judged('{"pass": false, "reason": "no"}'), False),
        (judged('{"pass": "false"}'), None),  # a string isn't a verdict
        (judged("not json"), None),
        (judged('{"pa', stop="max_tokens"), None),
        (ApiResult(500, {"error": {"message": "boom"}}), None),
    ],
)
async def test_the_judge_gives_a_verdict_or_none(result: ApiResult, expected: bool | None) -> None:
    class OneAnswer:
        def __init__(self) -> None:
            self.sent: dict[str, Any] = {}

        async def send(self, request: dict[str, Any], betas: tuple[str, ...]) -> ApiResult:
            self.sent = request
            return result

    api = OneAnswer()
    verdict, _ = await tier2_api.judge(api, "judge", "q?", "rubric", "an answer with </grading> in it")

    assert verdict is expected
    assert "thinking" not in api.sent  # current models reject {"type": "disabled"}
    assert api.sent["output_config"]["effort"] == "low"  # thinking stays short instead
    assert api.sent["messages"][0]["content"].count("</grading>") == 1  # the answer can't close the tag


async def test_a_failed_judge_call_keeps_the_api_s_explanation() -> None:
    class Rejects:
        async def send(self, request: dict[str, Any], betas: tuple[str, ...]) -> ApiResult:
            return ApiResult(400, {"error": {"message": "thinking.type.disabled is not supported"}})

    verdict, reason = await tier2_api.judge(Rejects(), "judge", "q?", "rubric", "answer")

    assert verdict is None
    assert reason == "judge failed: HTTP 400: thinking.type.disabled is not supported"


def test_usage_sums_responses_and_counts_dropped_thinking() -> None:
    usage = Usage()
    usage.add(
        {
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 3,
                "output_tokens": 7,
            },
            "input_transformations": [
                {"type": "thinking_dropped"},
                {"type": "something_else"},
                {"type": "thinking_dropped"},
            ],
        }
    )
    usage.add({"usage": {"input_tokens": 1}})

    assert (usage.input_tokens, usage.cache_read, usage.cache_write, usage.output_tokens) == (11, 5, 3, 7)
    assert usage.thinking_dropped == 2 and usage.total_input == 19
    assert (usage + Usage(input_tokens=1)).input_tokens == 12


# --- The real HTTP client, on a fake transport ----------------------------------------------------------


async def test_the_http_client_sends_exact_bytes_and_retries() -> None:
    seen: list[httpx.Request] = []
    answers = [httpx.Response(529, json={"error": {"message": "overloaded"}}), httpx.Response(200, json={"ok": 1})]

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers.pop(0)

    api = HttpMessagesApi("test-key", transport=httpx.MockTransport(handler), delays=(0, 0))
    request = {"model": "m", "messages": [{"role": "user", "content": "héllo"}]}

    result = await api.send(request, ("beta-one", "beta-two"))

    assert result == ApiResult(200, {"ok": 1})
    assert len(seen) == 2  # the 529 was tried again
    assert seen[1].content == tier2.pipeline.encode_request(request)  # Liber's exact bytes
    assert seen[1].headers["x-api-key"] == "test-key" and seen[1].headers["anthropic-beta"] == "beta-one,beta-two"
    assert "test-key" not in seen[1].content.decode()


async def test_the_http_client_reports_what_it_cannot_retry_away() -> None:
    async def not_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="<html>bad gateway page</html>")

    async def no_network(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    bad = await HttpMessagesApi("k", transport=httpx.MockTransport(not_json), delays=()).send({}, ())
    down = await HttpMessagesApi("k", transport=httpx.MockTransport(no_network), delays=(0,)).send({}, ())

    assert bad.status == 400 and "bad gateway" in bad.body["error"]["message"]
    assert down.status == 0 and "ConnectError" in down.body["error"]["message"]


# --- The command line ------------------------------------------------------------------------------------


def test_it_refuses_to_run_without_live_in_ci_or_without_a_key(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("CI", raising=False)

    assert tier2.main([]) == 2 and "--live" in capsys.readouterr().out
    assert tier2.main(["--live", "--repeats", "0"]) == 2 and "--repeats" in capsys.readouterr().out
    monkeypatch.setenv("CI", "true")
    assert tier2.main(["--live"]) == 2 and "never runs in CI" in capsys.readouterr().out
    monkeypatch.delenv("CI")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert tier2.main(["--live"]) == 2 and "ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_the_task_fixtures_load_and_one_is_reasoning_dependent() -> None:
    tasks = tier2.load_tasks(TASKS)

    assert any(task.reasoning_dependent and task.evidence_markers for task in tasks)
    for task in tasks:
        workspace_tools = {"read_file", "write_file"} if task.files else set()
        assert not set(task.tool_outputs) & workspace_tools  # the workspace would shadow them silently
        served = set(task.tool_outputs) | workspace_tools
        assert task.needle_fact and task.probe_rubric and served == {t["name"] for t in task.tools}


# --- The report on a fixed dataset --------------------------------------------------------------------


def run(
    task: str, condition: str, repeat: int, success: bool | None, tokens: int, needle: bool | None = True, **extra: Any
) -> dict[str, Any]:
    usage = {
        "input_tokens": tokens // 2,
        "cache_read": tokens // 2,
        "cache_write": 0,
        "output_tokens": 50,
        "thinking_dropped": 0,
    }
    record = {
        "task": task,
        "condition": condition,
        "repeat": repeat,
        "reasoning_dependent": task == "t1",
        "success": success,
        "needle_kept": needle,
        "usage": usage,
        "overhead": {k: 0 for k in usage},
        "cost": tokens / 1_000_000,
        "binding_errors": [],
        "errors": [],
        "liber_outcomes": [],
        "thinking_with_text": 1,
        "frozen_blocks": [],
        "summary_has_evidence": None,
        "answers": {},
        "judge_reasons": {},
    }
    return {**record, **extra}


# t1 (reasoning): success A 3/3, B 1/3, C 2/3, D 0/3; tokens A 1000, B 600, C 650, D 300.
# t2 (factual):   success A 2/2, B 2/2, C 2/2, D 1/2; tokens A 2000, B 1000, C 1000, D 500.
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
    both = {c.name: c for c in tier2_report.overall(per_task)}

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
    assert both["A vs B"].success_delta == pytest.approx((2 / 3 + 0) / 2)
    assert both["B vs D"].success_delta == pytest.approx((1 / 3 + 0.5) / 2)
    assert both["C vs B"].input_change == pytest.approx((650 / 600 - 1 + 0) / 2)


def test_the_report_shows_mean_spread_and_how_many_verdicts() -> None:
    stats = tier2_report.summarize(FIXED)["t1"]
    text = tier2_report.report(FIXED, {"date": "d", "repeats": 3})

    assert stats["B"].success.mean == pytest.approx(1 / 3)
    assert (stats["B"].success.low, stats["B"].success.high) == (0.0, 1.0)
    assert stats["B"].success.stdev == pytest.approx(0.57735, rel=1e-4)
    assert stats["D"].needle.mean == 0.0
    assert stats["A"].input_tokens.mean == 1000 and stats["A"].input_tokens.stdev == 0.0
    assert "33% (0% to 100%, sd 58%) [3/3]" in text
    # Reasoning-dependent tasks are averaged apart from the others.
    assert "## Comparisons, averaged over reasoning-dependent tasks (t1)" in text
    assert "## Comparisons, averaged over other tasks (t2)" in text
    assert "| A vs B | what dropping thinking costs | +67% | +67% |" in text


def test_missing_verdicts_and_errored_runs_are_counted_honestly() -> None:
    runs = [
        run("t", "A", 1, True, 1000),
        run("t", "A", 2, None, 400, needle=None, errors=["HTTP 500: overloaded"]),  # partial usage
        run("t", "B", 1, False, 600, binding_errors=["messages.3.content.0: bound elsewhere"]),
    ]  # no C or D runs at all

    stats = tier2_report.summarize(runs)["t"]
    text = tier2_report.report(runs, {"date": "d", "repeats": 2})

    assert stats["A"].success.n == 1 and stats["A"].runs == 2  # the missing verdict is left out...
    assert "100% [1/2]" in text  # ...and the page says so
    assert stats["A"].input_tokens.mean == 1000  # the errored run's partial tokens aren't averaged in
    assert stats["C"].success.mean is None and stats["C"].runs == 0
    assert "## Binding errors (by failing path)" in text and "messages.3.content.0" in text
    assert "## Other errors" in text and "HTTP 500" in text


def test_overhead_counts_toward_input_tokens() -> None:
    record = run("t", "D", 1, True, 300)
    record["overhead"] = {**record["overhead"], "input_tokens": 700}

    assert tier2_report.total_input(record) == 1000


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
    assert [(task, date) for date, task, _ in points] == [
        ("t1", "2026-11-07-110000"),
        ("t2", "2026-11-07"),
        ("t2", "2026-11-07-110000"),
    ]
    assert points[1][2]["D"] == (0.5, 500.0)
    assert "## Trends" in text and "## t1 (reasoning-dependent)" in text and "## t2 (factual)" in text


# --- --rejudge: grade a saved run's answers again --------------------------------------------------


class KeywordJudge:
    """Passes an answer that contains "good"; counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def send(self, request: dict[str, Any], betas: tuple[str, ...]) -> ApiResult:
        self.calls += 1
        grading = json.loads(request["messages"][0]["content"].removeprefix("<grading>\n").removesuffix("\n</grading>"))
        reply = json.dumps({"pass": "good" in grading["answer"], "reason": "keyword"})
        return ApiResult(200, {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn"})


def saved_run(condition: str, probe: str | None, needle: str | None, **extra: Any) -> dict[str, Any]:
    """A run as a history file stores it (every field), with the judge's verdicts missing."""
    answers = {k: v for k, v in (("probe", probe), ("needle", needle)) if v is not None}
    run = asdict(ConditionRun("dead_end_debugging", condition, 1, True))
    run.update(answers=answers, judge_reasons={k: "judge failed: HTTP 400" for k in answers}, **extra)
    return run


async def test_rejudge_grades_the_saved_answers_without_rerunning_anything() -> None:
    tasks = {task.name: task for task in tier2.load_tasks(TASKS)}
    judge = KeywordJudge()
    runs = [
        saved_run("A", "good answer", "good fact"),
        saved_run("B", "bad answer", "good fact"),
        saved_run("C", None, None, errors=["repeat failed: boom"]),  # nothing was answered
        saved_run("D", None, "good fact", binding_errors=["messages.3.content.0: bound to other history"]),
    ]

    graded = await tier2.rejudge(judge, tasks, "judge", runs)

    assert [(r["success"], r["needle_kept"]) for r in graded] == [
        (True, True),
        (False, True),
        (None, None),
        (False, True),
    ]
    assert judge.calls == 5  # one per saved answer: no conversation was re-run
    assert graded[2]["judge_reasons"] == {}  # untouched
    assert runs[0]["success"] is None  # the input isn't modified


async def test_rejudge_file_rewrites_the_run_in_place_and_keeps_the_original(tmp_path: Path) -> None:
    path = tmp_path / "2026-11-08.json"
    original = {"model": "m", "judge_model": "old", "summary_model": "s", "repeats": 1, "date": "2026-11-08"}
    original["runs"] = [saved_run("A", "good answer", "good fact")]
    path.write_text(json.dumps(original), encoding="utf-8")

    message = await tier2.rejudge_file(
        KeywordJudge(), path, TASKS, "claude-opus-5-5", datetime(2026, 11, 8, tzinfo=UTC)
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert message.endswith("1 runs, 2 pass, 0 fail, 0 missing")
    assert data["runs"][0]["success"] is True and data["judge_model"] == "claude-opus-5-5"
    assert data["rejudged"] == [
        {"at": "2026-11-08T00:00:00+00:00", "judge_model": "claude-opus-5-5", "previous_judge_model": "old"}
    ]
    assert json.loads((tmp_path / "2026-11-08.json.bak").read_text(encoding="utf-8")) == original
    assert "100%" in (tmp_path / "2026-11-08.md").read_text(encoding="utf-8")
    assert json_files(tmp_path) == ["2026-11-08.json"]  # the backup isn't a trend point


def json_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.glob("*.json"))


async def test_a_second_rejudge_keeps_the_true_original(tmp_path: Path) -> None:
    path = tmp_path / "2026-11-08.json"
    original = {"judge_model": "old", "date": "2026-11-08", "runs": [saved_run("A", "good answer", "good fact")]}
    path.write_text(json.dumps(original), encoding="utf-8")

    for day in (8, 9):
        await tier2.rejudge_file(KeywordJudge(), path, TASKS, "new", datetime(2026, 11, day, tzinfo=UTC))

    assert json.loads((tmp_path / "2026-11-08.json.bak").read_text(encoding="utf-8")) == original
    rejudged = json.loads(path.read_text(encoding="utf-8"))["rejudged"]
    assert [entry["previous_judge_model"] for entry in rejudged] == ["old", "new"]


async def test_rejudge_leaves_runs_it_cannot_grade_fairly_and_says_why() -> None:
    tasks = {task.name: task for task in tier2.load_tasks(TASKS)}
    task = tasks["dead_end_debugging"]
    judge = KeywordJudge()
    missing = {**saved_run("A", "good answer", None), "task": "deleted_task"}
    old_question = saved_run("B", "good answer", None, asked={"probe": "an older question", "needle": "?"})
    same_question = saved_run(
        "C", "good answer", None, asked={"probe": task.probe_question, "needle": task.needle_question}
    )
    stale_note = saved_run("D", "good answer", None)
    stale_note["judge_reasons"]["rejudge"] = "an earlier note"

    graded = await tier2.rejudge(judge, tasks, "judge", [missing, old_question, same_question, stale_note])

    assert judge.calls == 2  # only C and D
    assert [r["success"] for r in graded] == [None, None, True, True]
    assert [r["rejudged"] for r in graded] == [False, False, True, True]
    assert "no task file named deleted_task" in graded[0]["judge_reasons"]["rejudge"]
    assert "the probe question changed" in graded[1]["judge_reasons"]["rejudge"]
    assert "rejudge" not in graded[3]["judge_reasons"]  # the earlier note no longer applies


class NoClient:
    def __init__(self, key: str) -> None:
        raise AssertionError("no API client may be made before the checks pass")


def test_rejudge_needs_live_and_a_history_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tier2, "HttpMessagesApi", NoClient)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    not_history = tmp_path / "other.json"
    not_history.write_text('{"something": "else"}', encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")

    assert tier2.main(["--rejudge", str(not_history)]) == 2  # no --live
    assert tier2.main(["--live", "--rejudge", str(tmp_path / "missing.json")]) == 2
    assert tier2.main(["--live", "--rejudge", str(tmp_path)]) == 2  # a directory
    assert tier2.main(["--live", "--rejudge", str(not_history)]) == 2
    assert tier2.main(["--live", "--rejudge", str(broken)]) == 2
    monkeypatch.setenv("CI", "true")
    assert tier2.main(["--live", "--rejudge", str(not_history)]) == 2


async def test_a_tool_call_during_the_questions_gets_nothing_and_is_reported() -> None:
    task = task_named("undiscussed_config_detail")
    sent: list[list[dict[str, Any]]] = []

    async def rereads_the_config(messages: list[dict[str, Any]]) -> ApiResult:
        sent.append(json.loads(json.dumps(messages)))
        if isinstance(messages[-1]["content"], str):  # the question: try to cheat
            call = {"type": "tool_use", "id": f"t{len(messages)}", "name": "read_config", "input": {}}
            return ApiResult(200, {"content": [call], "stop_reason": "tool_use"})
        return ApiResult(200, {"content": [{"type": "text", "text": "I don't know"}], "stop_reason": "end_turn"})

    run = ConditionRun(task.name, "B", 1)
    probe, needle = await tier2.questions(rereads_the_config, [], task, run, {})

    assert probe == needle == "I don't know"
    assert run.refused_tool_calls == ["read_config", "read_config"]
    assert "backoff_multiplier = 1.7" not in json.dumps(sent)  # the config never came back
    [result] = sent[1][-1]["content"]
    assert result["is_error"] is True and result["content"] == tier2.TOOLS_UNAVAILABLE
    report = tier2_report.report([asdict(run)], {"model": "m", "judge_model": "j", "repeats": 1})
    assert "## Tool calls refused during the questions" in report
    assert "B #1: read_config, read_config" in report


# --- Condition E: chosen items kept through the summary (docs/decisions/0016) -------------------------


async def test_e_keeps_the_chosen_tool_result_and_recovers_what_every_summary_lost() -> None:
    task = task_named("obscure_setting_in_big_config")

    runs = await tier2.run_eval(FakeApi(), worker_model(), [task], Settings(SETTINGS.model, "j", repeats=1), quiet)

    by = {r.condition: r for r in runs}
    assert set(by) == {"A", "B", "C", "D", "E"}
    # Only A (everything) and E (the config kept as it was) can still read the undiscussed value.
    assert {c: by[c].success for c in by} == {"A": True, "B": False, "C": False, "D": False, "E": True}
    assert all(r.errors == [] and r.binding_errors == [] for r in runs)
    e = by["E"]
    [block] = e.frozen_blocks
    assert "Kept as is" in block and "[tool_result of read_config, message 2]" in block
    assert "stale_while_revalidate_s = 35" in block and "stale_while_revalidate_s = 35" not in by["B"].frozen_blocks[0]
    assert e.liber_outcomes == ["rendered", "rendered"]
    # The kept item costs tokens: E sends more than B, still less than A.
    assert by["B"].usage.input_tokens < e.usage.input_tokens < by["A"].usage.input_tokens


async def test_tasks_without_kept_tools_dont_run_e(dead_end: tier2.Task) -> None:
    runs = await tier2.run_eval(FakeApi(), worker_model(), [dead_end], Settings(SETTINGS.model, "j", repeats=1), quiet)
    text = tier2_report.report([asdict(r) for r in runs], {"date": "d", "repeats": 1})

    assert {r.condition for r in runs} == {"A", "B", "C", "D"}
    assert "| E: Liber, chosen items kept | n/a [0/0]" in text


async def test_e_with_nothing_to_keep_is_an_error_not_a_silent_b(monkeypatch: pytest.MonkeyPatch) -> None:
    task = task_named("obscure_setting_in_big_config")
    monkeypatch.setattr(tier2, "kept_results", lambda *args: ())
    monkeypatch.setattr(tier2, "kept_replies", lambda *args: ())

    runs = await tier2.run_eval(FakeApi(), worker_model(), [task], Settings(SETTINGS.model, "j", repeats=1), quiet)

    e = next(r for r in runs if r.condition == "E")
    assert e.errors and e.errors[0].startswith("nothing to keep")
    assert e.success is None and e.answers == {} and e.usage.input_tokens == 0  # nothing was asked


def test_the_report_has_the_three_e_comparisons() -> None:
    runs = [
        run("t", c, 1, ok, tokens)
        for c, ok, tokens in (("A", True, 1000), ("B", False, 400), ("D", False, 300), ("E", True, 500))
    ]
    runs.append(run("t", "C", 1, False, 450))

    comparisons = {c.name: c for c in tier2_report.compare(tier2_report.summarize(runs)["t"])}

    assert comparisons["A vs E"].success_delta == 0.0 and comparisons["A vs E"].input_change == pytest.approx(1.0)
    assert comparisons["E vs B"].success_delta == 1.0 and comparisons["E vs B"].input_change == pytest.approx(0.25)
    assert comparisons["E vs D"].success_delta == 1.0 and comparisons["E vs D"].input_change == pytest.approx(
        500 / 300 - 1
    )


async def test_a_refused_plan_asks_nothing_and_gets_no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-11-08: E's plan was refused on 4 of 5 repeats, the questions went out unedited (condition A),
    and their passes were counted as E's. Now a refused plan ends the condition with no verdict."""
    task = task_named("obscure_setting_in_big_config")
    original = tier2.commands.create

    async def refuse_e(store: Any, session_id: str, *args: Any, **kwargs: Any) -> str:
        if kwargs.get("pin"):
            raise tier2.PlanError("this plan wouldn't make the conversation smaller")
        return await original(store, session_id, *args, **kwargs)

    monkeypatch.setattr(tier2.commands, "create", refuse_e)
    api = FakeApi()

    runs = await tier2.run_eval(api, worker_model(), [task], Settings(SETTINGS.model, "j", repeats=1), quiet)
    text = tier2_report.report([asdict(r) for r in runs], {"date": "d", "repeats": 1})

    e = next(r for r in runs if r.condition == "E")
    assert e.success is None and e.needle_kept is None and e.answers == {}
    assert e.errors == ["plan refused: this plan wouldn't make the conversation smaller"]
    assert e.usage.input_tokens == 0  # nothing was sent for E
    assert "| E: Liber, chosen items kept | n/a [0/1]" in text


async def test_an_unedited_liber_request_is_never_sent(dead_end: tier2.Task) -> None:
    """With a model Liber can't confirm, the probe still goes out compacted (all its thinking was stale
    and left out), but the needle request carries the probe reply's thinking, so Liber would send it
    unedited: that one isn't sent, and gets no verdict."""
    api = FakeApi()

    runs = await tier2.run_eval(api, worker_model(), [dead_end], Settings("some-other-model", "j", repeats=1), quiet)

    b = next(r for r in runs if r.condition == "B")
    assert b.liber_outcomes[0] == "rendered" and b.liber_outcomes[1].startswith("passthrough")
    assert b.success is not None and "probe" in b.answers  # the compacted probe was asked and graded
    assert b.needle_kept is None and "needle" not in b.answers  # the unedited needle request wasn't sent
    assert any(e.startswith("Liber didn't send the compaction: passthrough") for e in b.errors)


async def test_big_config_task_plumbing_e_is_accepted_and_keeps_the_obscure_value() -> None:
    """Pinning's intended shape: the config is a small part of what's summarized, so E's plan is
    accepted, E keeps the undiscussed value, and still sends far less than full context."""
    task = task_named("obscure_setting_in_big_config")

    runs = await tier2.run_eval(FakeApi(), worker_model(), [task], Settings(SETTINGS.model, "j", repeats=1), quiet)

    by = {r.condition: r for r in runs}
    assert {c: by[c].success for c in by} == {"A": True, "B": False, "C": False, "D": False, "E": True}
    e = by["E"]
    assert e.errors == [] and e.liber_outcomes == ["rendered", "rendered"]
    assert "stale_while_revalidate_s = 35" in e.frozen_blocks[0]  # the config kept...
    assert "status=504" not in e.frozen_blocks[0]  # ...the logs not
    assert e.usage.input_tokens < by["A"].usage.input_tokens / 2


async def test_e_on_code_reconstruction_keeps_the_assistants_code_reply() -> None:
    """The ADR 0017 control: E pins setup turn 2's reply, the one that printed the final parse()."""
    task = task_named("code_reconstruction")

    runs = await tier2.run_eval(FakeApi(), worker_model(), [task], Settings(SETTINGS.model, "j", repeats=1), quiet)

    by = {r.condition: r for r in runs}
    assert {c: by[c].success for c in by} == {"A": True, "B": False, "C": False, "D": False, "E": True}
    [block] = by["E"].frozen_blocks
    assert "[assistant text, message" in block and FINAL_CODE in block
    assert "# first edit" not in block  # only the chosen turn's reply, not the earlier version
    assert FINAL_CODE not in by["B"].frozen_blocks[0]


def test_kept_turns_must_be_inside_the_summary(tmp_path: Path) -> None:
    task = json.loads((TASKS / "code_reconstruction.json").read_text())
    task["keep_assistant_text_of_turns"] = [task["compact_through_turn"] + 1]
    (tmp_path / "bad.json").write_text(json.dumps(task))

    with pytest.raises(ValueError, match="turns the summary covers"):
        tier2.load_tasks(tmp_path)
    for bad in (-1, "2", True):
        task["keep_assistant_text_of_turns"] = [bad]
        (tmp_path / "bad.json").write_text(json.dumps(task))
        with pytest.raises(ValueError, match="turns the summary covers"):
            tier2.load_tasks(tmp_path)


def test_kept_replies_takes_the_assistants_text_in_the_chosen_turn_only() -> None:
    task = task_named("code_reconstruction")
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "turn 0"},
        {"role": "assistant", "content": [{"type": "text", "text": "zero"}]},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": [{"type": "text", "text": "one"}]},
        {"role": "user", "content": "turn 2"},  # a turn with a tool call in it
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "t", "name": "read_file", "input": {}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "file"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "def parse(): ..."}]},
        {"role": "user", "content": "turn 3"},
    ]
    base = tier2.base_request(task, SETTINGS.model)

    ids = tier2.kept_replies(task, history, base, starts=[0, 2, 4, 8])

    items = {item.id: item for item in tier2.parse({**base, "messages": history}).items}
    assert [items[i].block["text"] for i in ids] == ["Let me look.", "def parse(): ..."]  # preamble and reply


async def test_an_unsent_probe_means_no_questions_at_all(dead_end: tier2.Task, monkeypatch: pytest.MonkeyPatch) -> None:
    """If Liber wouldn't send even the probe compacted, neither question is sent."""
    original = tier2.pipeline.prepare

    async def falls_back_on_the_probe(store: Any, body: bytes, *args: Any, **kwargs: Any) -> Any:
        prepared = await original(store, body, *args, **kwargs)
        if dead_end.probe_question.encode() in body:
            prepared.outcome = "fallback"
        return prepared

    monkeypatch.setattr(tier2.pipeline, "prepare", falls_back_on_the_probe)
    api = FakeApi()

    runs = await tier2.run_eval(api, worker_model(), [dead_end], Settings(SETTINGS.model, "j", repeats=1), quiet)

    b = next(r for r in runs if r.condition == "B")
    assert b.liber_outcomes == ["fallback"]  # only the probe was prepared; the needle never asked
    assert b.success is None and b.needle_kept is None and b.answers == {} and b.usage.input_tokens == 0


async def test_rejudge_gives_no_verdict_to_old_runs_whose_plan_was_refused(dead_end: tier2.Task) -> None:
    old = run("dead_end_debugging", "E", 1, True, 900, errors=["plan refused: wouldn't make it smaller"])
    old["answers"] = {"probe": "ruled out: something", "needle": f"{NEEDLE}, Priya Raman"}
    valid = run("dead_end_debugging", "B", 1, None, 600, errors=["Liber didn't send the compaction: passthrough"])
    valid["answers"] = {"probe": f"ruled out: {REASONING}"}  # since 2026-11-09 the sent probe is valid

    graded = await tier2.rejudge(FakeApi(), {dead_end.name: dead_end}, "j", [old, valid])

    assert graded[0]["success"] is None and graded[0]["needle_kept"] is None
    assert "no verdict" in graded[0]["judge_reasons"]["rejudge"]
    assert graded[1]["success"] is True


# --- code_reconstruction_reread: the code is on disk and the model may look again -------------------


async def test_with_rereads_allowed_compacted_conditions_read_the_saved_file() -> None:
    """Plumbing with rigged fakes: setup's edits land in the workspace, and a condition that lost the
    code from context rereads it (and that reread is recorded and counted) instead of failing."""
    task = task_named("code_reconstruction_reread")

    runs = await tier2.run_eval(FakeApi(), worker_model(), [task], Settings(SETTINGS.model, "j", repeats=1), quiet)

    by = {r.condition: r for r in runs}
    assert {c: by[c].success for c in by} == {"A": True, "B": True, "C": True, "D": True}
    assert by["A"].question_tool_calls == []  # the code was still in its context
    for condition in "BCD":
        assert by[condition].question_tool_calls == ["read_file"]  # the reread, recorded
        assert by[condition].refused_tool_calls == []
    assert all(r.errors == [] and r.binding_errors == [] for r in runs)


def test_the_workspace_reads_and_writes_files() -> None:
    task = task_named("code_reconstruction_reread")
    workspace = dict(task.files)

    def call(name: str, **arguments: Any) -> str:
        return tier2.tool_output(task, workspace, {"name": name, "input": arguments})

    assert call("read_file", path="etl/parser.py").startswith('"""Parse the nightly orders export')
    assert call("read_file", path="missing.py") == "No such file: missing.py. Files: etl/parser.py"
    assert call("read_file", path="./etl/parser.py") == call("read_file", path="/etl/parser.py")  # one file
    assert call("write_file", path="etl/parser.py", content="new") == "Wrote 3 characters to etl/parser.py."
    assert call("read_file", path="etl/parser.py") == "new"
    assert call("write_file", path="etl/parser.py").startswith("write_file needs")
    assert task.files["etl/parser.py"] != "new"  # the task's own files are never changed


async def test_each_condition_gets_its_own_copy_of_the_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    task = task_named("code_reconstruction_reread")
    seen: list[dict[str, str]] = []
    original = tier2.questions

    async def scribbling(send: Any, messages: Any, task: Any, run: Any, workspace: dict[str, str]) -> Any:
        seen.append(dict(workspace))  # what this condition started from
        workspace[f"scribble-{run.condition}"] = "x"  # a write that must not reach the next condition
        return await original(send, messages, task, run, workspace)

    monkeypatch.setattr(tier2, "questions", scribbling)

    await tier2.run_eval(FakeApi(), worker_model(), [task], Settings(SETTINGS.model, "j", repeats=1), quiet)

    assert len(seen) == 3  # A, B and C go through questions(); D asks its own
    assert all(not any(path.startswith("scribble-") for path in start) for start in seen)
    assert all(EDITED in start["etl/parser.py"] for start in seen)  # setup's save, in every copy
    assert EDITED not in task.files["etl/parser.py"]  # and never in the task itself


async def test_tasks_with_files_still_refuse_tools_in_questions_unless_allowed() -> None:
    from dataclasses import replace

    task = replace(task_named("code_reconstruction_reread"), questions_may_use_tools=False)

    runs = await tier2.run_eval(FakeApi(), worker_model(), [task], Settings(SETTINGS.model, "j", repeats=1), quiet)

    b = next(r for r in runs if r.condition == "B")
    assert b.refused_tool_calls == ["read_file"] and b.question_tool_calls == []


async def test_a_setup_that_left_the_file_wrong_gets_no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    task = task_named("code_reconstruction_reread")
    monkeypatch.setattr(tier2, "workspace_problems", lambda task, workspace: ["setup left etl/parser.py without 'x'"])
    api = FakeApi()

    runs = await tier2.run_eval(api, worker_model(), [task], Settings(SETTINGS.model, "j", repeats=1), quiet)

    assert all(
        r.success is None and r.answers == {} and r.errors == ["setup left etl/parser.py without 'x'"] for r in runs
    )


def test_without_a_workspace_a_task_with_files_fails_loudly() -> None:
    with pytest.raises(ValueError, match="no workspace"):
        tier2.tool_output(task_named("code_reconstruction_reread"), None, {"name": "read_file", "input": {"path": "x"}})


async def test_the_report_counts_rereads_and_averages_those_tasks_apart() -> None:
    runs = await tier2.run_eval(
        FakeApi(),
        worker_model(),
        [task_named("code_reconstruction_reread")],
        Settings(SETTINGS.model, "j", repeats=1),
        quiet,
    )

    text = tier2_report.report([asdict(r) for r in runs], {"date": "d", "repeats": 1})

    assert "## Comparisons, averaged over tasks where the questions may reread" in text
    assert "averaged over other tasks" not in text  # the reread task isn't mixed into the others
    assert "- code_reconstruction_reread A: reread in 0 of 1 runs (0 tool calls)" in text
    assert "- code_reconstruction_reread B: reread in 1 of 1 runs (1 tool calls)" in text
