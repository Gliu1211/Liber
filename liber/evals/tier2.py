"""Tier 2 quality evals: what compaction costs in task success, against real models (ARCHITECTURE.md §12).

    ANTHROPIC_API_KEY=... uv run python -m liber.evals.tier2 --live [--repeats 3] [--model ...]
    ANTHROPIC_API_KEY=... uv run python -m liber.evals.tier2 --live --rejudge evals/history/<date>.json

The second form only grades a saved run's answers again (see `rejudge`): judge calls, nothing else.

Manual and paid: it refuses to run without `--live`, refuses under CI, and reads the key from the
environment only (hard rules 2, 7). Tests drive `run_eval` with fakes instead.

For each task (evals/tier2_tasks/*.json) and each repeat:

    1. setup        the task's turns, full context, thinking on (with its text returned); the
                    model calls the task's tools and gets their scripted output. One history,
                    shared by all four conditions, so they differ only in context handling.
    2. conditions   that history continued with two questions, the task's probe and then the
                    needle question, under four conditions (§12, ADR 0004 A5):
         A  full context: the history as is (the upper bound)
         B  Liber, thinking dropped: the early turns summarized by the worker, stale thinking
            left out (Liber's default)
         C  Liber + carry_forward_reasoning: B's summaries, plus the notes section
         D  client auto-compaction (simulated): the model summarizes the whole conversation, which
            is replaced by that summary (the status quo)
    3. judging      an LLM judge grades the probe answer against the task's rubric (task success)
                    and the needle answer against the planted fact (faithfulness).

How B and C are run (docs/decisions/0015): Liber's own request path runs in-process
(`pipeline.prepare` on a throwaway database, plans made with `plan/commands.py`, as the CLI makes
them), and the request it would send goes straight to the API. Every request, in every condition,
asks for `prefix_mismatch_behavior: "error"` (tier2_api.py), so a thinking block Liber failed to
leave out comes back as a 400 naming it. A binding break counts as a failed answer.

B and C get identical summaries: the worker writes them once per repeat, with notes, and both
plans are made from those rows; B's plan leaves the notes out (`commands.create`).

Results are saved after every repeat to evals/history/<date>.json with a markdown report beside it
(tier2_report.py), so a failure late in a paid run keeps what came before.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from liber import pipeline
from liber.cache import exchanges
from liber.config import DEFAULT_PRICING, DEFAULT_SUMMARY_MODEL, ModelPrice
from liber.evals import tier2_report
from liber.evals.tier2_api import (
    NOT_SENT,
    ApiResult,
    HttpMessagesApi,
    MessagesApi,
    Usage,
    binding_error,
    judge,
    reply_text,
    with_strict_binding,
)
from liber.model.anthropic import RecentRequests, parse
from liber.model.items import ExchangeSummary, Kind
from liber.plan import commands, cost, versions
from liber.plan.versions import PlanError
from liber.session.store import RequestRecord, Store
from liber.worker.client import ModelClient
from liber.worker.run import Job, Worker
from liber.worker.summarize import summarizer_id

TASKS_DIR = Path("evals/tier2_tasks")
HISTORY_DIR = Path("evals/history")
CONDITIONS = tier2_report.CONDITIONS  # ("A", "B", "C", "D", "E")
MAX_TOOL_ROUNDS = 5  # per turn: a model still calling tools after this many rounds is an error
MAX_TOKENS = 4000

# The client-side compaction D simulates: the model writes the summary, which replaces the history.
COMPACT_PROMPT = (
    "Your context window is full. Write a summary of the conversation so far that lets you continue "
    "the work without it: the user's requests, what was found and decided and why, and every fact "
    "the user asked you to remember. Reply with the summary only."
)
CONTINUED = "This session continues an earlier conversation, summarized here:\n\n{summary}\n\n{prompt}"


@dataclass(frozen=True)
class Task:
    name: str
    description: str
    reasoning_dependent: bool
    system: str
    tools: list[dict[str, Any]]
    tool_outputs: dict[str, str]
    turns: list[str]
    compact_through_turn: int  # B, C and D compact turns 0..this one
    needle_fact: str
    needle_question: str
    probe_question: str
    probe_rubric: str
    # Strings that would let a summary answer the probe without the thinking. If B's summary has one,
    # B's result says how lossy the summary was, not what dropping thinking cost (the report shows it).
    evidence_markers: list[str] = field(default_factory=list)
    # Condition E (docs/decisions/0016): the tools whose results a user would keep through the
    # summary. Item ids differ per run, so the task names tools; E pins their results in the
    # summarized range. Tasks without it don't run E.
    keep_tool_results: list[str] = field(default_factory=list)
    # A simulated workspace: path -> content. With it, `read_file` returns a file's current content
    # and `write_file` replaces it, so edits made during setup are on "disk" afterwards. Each
    # condition starts from its own copy of the workspace as setup left it.
    files: dict[str, str] = field(default_factory=dict)
    # Whether the probe and needle questions may use tools (served as in setup). Off by default: most
    # tasks test what survives in context, and a reread would hide that. On, the task measures the
    # realistic case where the model can look again, and what that costs (its extra requests count).
    questions_may_use_tools: bool = False
    # path -> strings setup must have written there (checked after setup; see workspace_problems).
    workspace_checks: dict[str, list[str]] = field(default_factory=dict)

    def conditions(self) -> tuple[str, ...]:
        return CONDITIONS if self.keep_tool_results else tuple(c for c in CONDITIONS if c != "E")


def load_tasks(directory: Path = TASKS_DIR) -> list[Task]:
    tasks = [Task(**json.loads(path.read_text(encoding="utf-8"))) for path in sorted(directory.glob("*.json"))]
    for task in tasks:
        if not 0 <= task.compact_through_turn < len(task.turns) - 1:
            raise ValueError(f"{task.name}: compact_through_turn must leave at least one turn after it")
    return tasks


@dataclass
class ConditionRun:
    """One condition of one repeat of one task: what was asked, answered, graded and paid."""

    task: str
    condition: str
    repeat: int
    reasoning_dependent: bool = False
    success: bool | None = None  # the judge's verdict on the probe; None: nothing to grade
    needle_kept: bool | None = None
    usage: Usage = field(default_factory=Usage)
    overhead: Usage = field(default_factory=Usage)  # D's compaction call (B/C's worker calls aren't metered)
    cost: float | None = None  # input side, dollars, usage + overhead (see `input_cost`)
    binding_errors: list[str] = field(default_factory=list)  # "path: message" of 400s about thinking
    errors: list[str] = field(default_factory=list)  # anything else that went wrong
    liber_outcomes: list[str] = field(default_factory=list)  # B/C: what Liber did with each request
    thinking_with_text: int = 0  # setup thinking blocks that came back with text (C needs some)
    frozen_blocks: list[str] = field(default_factory=list)  # B/C: the summary blocks Liber sent
    summary_has_evidence: bool | None = None  # B/C: a frozen block contains an evidence marker
    answers: dict[str, str] = field(default_factory=dict)
    asked: dict[str, str] = field(default_factory=dict)  # the questions as asked, so a rejudge can check
    refused_tool_calls: list[str] = field(default_factory=list)  # tools the model tried during the questions
    question_tool_calls: list[str] = field(default_factory=list)  # tools used during the questions, where allowed
    tools_in_questions: bool = False  # the task allowed tools during the questions (rereads in the tokens)
    judge_reasons: dict[str, str] = field(default_factory=dict)


# --- One turn -----------------------------------------------------------------------------------------

Sender = Callable[[list[dict[str, Any]]], Awaitable[ApiResult]]


TOOLS_UNAVAILABLE = "Tools are unavailable now. Answer from memory, from what is already in this conversation."


async def turn(
    send: Sender,
    messages: list[dict[str, Any]],
    prompt: str,
    task: Task,
    run: ConditionRun | None,
    tools: bool = True,
    workspace: dict[str, str] | None = None,
) -> str | None:
    """One user turn on `messages` (extended in place), tool round trips included.

    The reply's text, or None if the turn failed: a rejected request, a reply cut off at
    max_tokens, or a model still calling tools after MAX_TOOL_ROUNDS (recorded on `run`).

    With `tools=False` (the probe and needle questions, unless the task allows tools there), a tool
    call gets an error result instead of the scripted output, and is recorded. Otherwise a model
    could pass a probe by reading the config or file again, and the judge, who sees only the
    answer, would never know. With `tools=True`, calls are served (`tool_output`, from `workspace`
    for a task with files) and recorded in `question_tool_calls` (meaningful for question turns).
    """
    messages.append({"role": "user", "content": prompt})
    for _ in range(MAX_TOOL_ROUNDS):
        result = await send(messages)
        if result.status == NOT_SENT:
            if run is not None:
                run.errors.append(str((result.body.get("error") or {}).get("message", "not sent")))
            return None
        if result.status != 200:
            problem = binding_error(result)
            if run is not None and problem is not None:
                run.binding_errors.append(problem)
            elif run is not None:
                run.errors.append(f"HTTP {result.status}: {str(result.body)[:200]}")
            return None
        if run is not None:
            run.usage.add(result.body)
        # The client echoes the reply exactly, thinking included (preserved thinking).
        content = result.body.get("content") or []
        messages.append({"role": "assistant", "content": content})
        stop = result.body.get("stop_reason")
        if stop == "max_tokens":
            if run is not None:
                run.errors.append("a reply was cut off at max_tokens")
            return None
        if stop != "tool_use":
            return reply_text(result.body)
        calls = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if tools:
            results = [
                {"type": "tool_result", "tool_use_id": b["id"], "content": tool_output(task, workspace, b)}
                for b in calls
            ]
            if run is not None:
                run.question_tool_calls += [b["name"] for b in calls]
        else:
            results = [
                {"type": "tool_result", "tool_use_id": b["id"], "content": TOOLS_UNAVAILABLE, "is_error": True}
                for b in calls
            ]
            if run is not None:
                run.refused_tool_calls += [b["name"] for b in calls]
        messages.append({"role": "user", "content": results})
    if run is not None:
        run.errors.append(f"the model was still calling tools after {MAX_TOOL_ROUNDS} rounds")
    return None


def tool_output(task: Task, workspace: dict[str, str] | None, call: dict[str, Any]) -> str:
    """What a tool call returns: the workspace for read_file/write_file (if the task has files), else
    the task's scripted output."""
    name = call.get("name")
    given = call.get("input")
    arguments: dict[str, Any] = given if isinstance(given, dict) else {}
    if task.files and name in ("read_file", "write_file"):
        if workspace is None:  # a caller forgot it: fail loudly rather than serve "(no output)"
            raise ValueError(f"{task.name} has files, but this turn was given no workspace")
        path = workspace_path(str(arguments.get("path", "")))
        if name == "read_file":
            if path in workspace:
                return workspace[path]
            return f"No such file: {path}. Files: {', '.join(sorted(workspace))}"
        content = arguments.get("content")
        if not isinstance(content, str):
            return "write_file needs a path and the file's full content as a string."
        workspace[path] = content
        return f"Wrote {len(content)} characters to {path}."
    return task.tool_outputs.get(str(name), "(no output)")


def workspace_path(path: str) -> str:
    """`./etl/parser.py` and `/etl/parser.py` name the same file as `etl/parser.py`."""
    path = path.strip()
    while path.startswith(("./", "/")):
        path = path.removeprefix("./").removeprefix("/")
    return path


def workspace_problems(task: Task, workspace: dict[str, str]) -> list[str]:
    """What setup should have left in the workspace but didn't (the task's `workspace_checks`).

    If the model skipped or botched a save, the file and the conversation disagree, and a reread
    would fail for reasons that have nothing to do with compaction.
    """
    problems = []
    for path, markers in task.workspace_checks.items():
        missing = [marker for marker in markers if marker not in workspace.get(path, "")]
        if missing:
            problems.append(f"setup left {path} without {', '.join(repr(m) for m in missing)}")
    return problems


# --- The four conditions ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    model: str
    judge_model: str
    summary_model: str = DEFAULT_SUMMARY_MODEL
    repeats: int = 3
    pricing: dict[str, ModelPrice] = field(default_factory=lambda: dict(DEFAULT_PRICING))


def base_request(task: Task, model: str) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": task.system,
        "tools": task.tools,
        # "summarized": thinking comes back with its text (current models omit it by default), which
        # carry_forward_reasoning needs; without it C could only ever equal B.
        "thinking": {"type": "adaptive", "display": "summarized"},
    }


def direct_sender(api: MessagesApi, base: dict[str, Any]) -> Sender:
    """A and D: the client's request, as is (with strict binding)."""

    async def send(messages: list[dict[str, Any]]) -> ApiResult:
        request, betas = with_strict_binding({**base, "messages": messages})
        return await api.send(request, betas)

    return send


def liber_sender(api: MessagesApi, base: dict[str, Any], store: Store, run: ConditionRun) -> Sender:
    """B and C: what Liber's request path would send for the client's request (with strict binding)."""
    recent = RecentRequests()

    async def send(messages: list[dict[str, Any]]) -> ApiResult:
        prepared = await pipeline.prepare(store, pipeline.encode_request({**base, "messages": messages}), recent)
        outcome = prepared.outcome if not prepared.edit_skipped else f"{prepared.outcome}: {prepared.edit_skipped}"
        run.liber_outcomes.append(outcome)
        if prepared.outcome != "rendered":  # unedited, it would be condition A: don't send it
            return ApiResult(NOT_SENT, {"error": {"message": f"Liber didn't send the compaction: {outcome}"}})
        request, betas = with_strict_binding(json.loads(prepared.body))
        return await api.send(request, betas)

    return send


async def setup_history(
    api: MessagesApi, task: Task, settings: Settings
) -> tuple[list[dict[str, Any]], list[int], dict[str, str]]:
    """The task's turns with full context: the shared history, where each turn starts, and the
    workspace as setup left it."""
    send = direct_sender(api, base_request(task, settings.model))
    messages: list[dict[str, Any]] = []
    starts = []
    workspace = dict(task.files)
    for prompt in task.turns:
        starts.append(len(messages))
        failed = ConditionRun(task.name, "setup", 0)
        if await turn(send, messages, prompt, task, failed, workspace=workspace) is None:
            raise RuntimeError(f"a setup turn failed: {(failed.binding_errors + failed.errors)[:1]}")
    return messages, starts, workspace


def thinking_with_text(messages: list[dict[str, Any]]) -> int:
    return sum(
        1
        for message in messages
        if message["role"] == "assistant"
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "thinking" and str(block.get("thinking") or "").strip()
    )


async def worker_summaries(
    task: Task, settings: Settings, history: list[dict[str, Any]], worker_client: ModelClient, directory: Path
) -> list[tuple[str, ExchangeSummary]]:
    """The worker's summaries of the history's exchanges, with notes: written once, shared by B and C."""
    store = Store(directory / "summaries.db")
    await store.create_tables()
    try:
        parsed = parse({**base_request(task, settings.model), "messages": history})
        worker = Worker(store, worker_client, settings.summary_model, carry_forward_reasoning=True)
        await worker.process(Job("tier2", settings.model, tuple(parsed.items)))
        keys = [e.key for e in exchanges.completed_exchanges(parsed.items)]
        summarizer = summarizer_id(settings.summary_model)
        written = await store.run(lambda db: exchanges.cached_summaries(db.connection(), keys, summarizer))
    finally:
        await store.close()
    return list(written.items())


def copy_summaries(db: Session, summaries: list[tuple[str, ExchangeSummary]], summarizer: str) -> None:
    now = datetime.now(UTC)
    for key, summary in summaries:
        exchanges.record_summary(db.connection(), key, summarizer, summary, now)


async def run_liber(
    api: MessagesApi,
    task: Task,
    settings: Settings,
    history: list[dict[str, Any]],
    through: int,
    summaries: list[tuple[str, ExchangeSummary]],
    run: ConditionRun,
    directory: Path,
    workspace: dict[str, str],
) -> tuple[str | None, str | None]:
    """B, C or E: plan from the shared summaries, applied at the probe (a clean turn), then both questions.

    E is B's plan plus pins: the results of the task's `keep_tool_results` tools in the range.
    """
    base = base_request(task, settings.model)
    store = Store(directory / f"{run.condition}.db")
    await store.create_tables()
    try:
        first = await pipeline.prepare(store, pipeline.encode_request({**base, "messages": history}))
        if first.session_id is None:
            run.errors.append(f"Liber didn't identify the conversation: {first.error}")
            return None, None
        session = first.session_id
        summarizer = summarizer_id(settings.summary_model)
        await store.run(lambda db: copy_summaries(db, summaries, summarizer))
        pins: tuple[str, ...] = ()
        if run.condition == "E":
            pins = kept_results(task, history, base, through)
            if not pins:  # E without pins would be B: no questions, no verdict
                run.errors.append(f"nothing to keep: no {task.keep_tool_results} result in messages 0-{through}")
                return None, None
            # Pinned text is read from recorded requests, as the proxy records them.
            await store.add_request(recorded_request(session, {**base, "messages": history}))
        try:
            await commands.create(
                store,
                session,
                through,
                summary_model=settings.summary_model,
                carry_forward_reasoning=run.condition == "C",
                pin=pins,
            )
            await commands.apply(store, session)
        except PlanError as exc:
            # Without the plan the questions would go out unedited, as condition A. That holds for B, C
            # and E alike: the refusal is recorded (Other errors) and the condition gets no verdict.
            run.errors.append(f"plan refused: {exc}")
            return None, None
        state = await versions.load_state(store, session)
        plan = state.pending.plan if state.pending is not None else state.applied.plan
        run.frozen_blocks = [op.summary for op in plan.operations if op.summary is not None]
        if task.evidence_markers:
            text = "\n".join(run.frozen_blocks).lower()
            run.summary_has_evidence = any(marker.lower() in text for marker in task.evidence_markers)
        return await questions(liber_sender(api, base, store, run), list(history), task, run, workspace)
    finally:
        await store.close()


def kept_results(task: Task, history: list[dict[str, Any]], base: dict[str, Any], through: int) -> tuple[str, ...]:
    """E's pins: the ids of the task's chosen tools' results in messages 0..through."""
    items = parse({**base, "messages": history}).items
    calls = {item.id: item.block.get("name") for item in items if item.kind is Kind.TOOL_USE}
    return tuple(
        item.id
        for item in items
        if item.kind is Kind.TOOL_RESULT
        and item.msg_index <= through
        and calls.get(item.depends_on or "") in task.keep_tool_results
    )


def recorded_request(session_id: str, request: dict[str, Any]) -> RequestRecord:
    """The record the proxy would keep for this request (body included, LIBER_RECORD_BODIES's default).

    Only E records one (its pins are read from it). Its `model` also gives the session a recorded
    model, which only matters to estimates if exact token counts are cached; in Tier 2 they aren't.
    """
    return RequestRecord(
        created_at=datetime.now(UTC),
        method="POST",
        path="/v1/messages",
        model=request.get("model"),
        session_id=session_id,
        request_body=pipeline.encode_request(request).decode("utf-8"),
    )


async def run_client_compaction(
    api: MessagesApi,
    task: Task,
    settings: Settings,
    history: list[dict[str, Any]],
    run: ConditionRun,
    workspace: dict[str, str],
) -> tuple[str | None, str | None]:
    """D: the model summarizes the whole history; the client continues from that summary alone."""
    send = direct_sender(api, base_request(task, settings.model))
    result = await send([*history, {"role": "user", "content": COMPACT_PROMPT}])
    if result.status != 200:
        run.errors.append(f"compaction failed: HTTP {result.status}")
        return None, None
    run.overhead.add(result.body)
    messages: list[dict[str, Any]] = []
    first = CONTINUED.format(summary=reply_text(result.body), prompt=task.probe_question)
    tools = task.questions_may_use_tools
    probe = await turn(send, messages, first, task, run, tools, workspace)
    needle = (
        await turn(send, messages, task.needle_question, task, run, tools, workspace) if probe is not None else None
    )
    return probe, needle


async def questions(
    send: Sender, messages: list[dict[str, Any]], task: Task, run: ConditionRun, workspace: dict[str, str]
) -> tuple[str | None, str | None]:
    """The probe, then (if it got an answer) the needle question in the same conversation.

    Tools stay offered (removing them would change the request). Unless the task allows tools here,
    calling one gets nothing back. `workspace`: this condition's own copy.
    """
    tools = task.questions_may_use_tools
    probe = await turn(send, messages, task.probe_question, task, run, tools, workspace)
    needle = (
        await turn(send, messages, task.needle_question, task, run, tools, workspace) if probe is not None else None
    )
    return probe, needle


def input_cost(usage: Usage, model: str, pricing: dict[str, ModelPrice]) -> float | None:
    """Input-side dollars at the pricing table's rates (output isn't in the table)."""
    price = cost.price_for(model, pricing)
    if price is None:
        return None
    rates = price.rates_for(usage.total_input)
    dollars = (
        usage.input_tokens * rates.input + usage.cache_write * rates.cache_write + usage.cache_read * rates.cache_read
    )
    return dollars / 1_000_000


def needle_rubric(task: Task) -> str:
    return f"Pass only if the answer states this fact correctly: {task.needle_fact}"


# Errors after which an older run's answers aren't this condition's (see rejudge). "Liber didn't send
# the compaction" isn't one: since 2026-11-09 an unsent question has no answer, and the rest are valid.
UNVERDICTED = ("plan refused", "nothing to keep")


async def rejudge(
    api: MessagesApi, tasks: dict[str, Task], judge_model: str, runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Grade an earlier run's saved answers again; no conversation is re-run, only the judge is paid.

    Why: the first live run's judge calls all failed (HTTP 400), but every answer was saved. Uses the
    task files' rubrics as they are now, so an edited rubric applies too. But an answer is only
    graded if its question is unchanged: an answer to an old question, graded against a new one,
    would mean nothing. (Runs saved before questions were recorded can't be checked.)

    Runs with no answers (a failed repeat) are left as they were. A binding break still counts as a
    failed answer, as in grade(). Each run that was graded again is marked `"rejudged": True`.
    """
    graded = []
    for saved in runs:
        run = {**saved, "judge_reasons": dict(saved.get("judge_reasons") or {})}
        run["judge_reasons"].pop("rejudge", None)  # a note from an earlier rejudge no longer applies
        run["rejudged"] = False
        task = tasks.get(run["task"])
        answers = run.get("answers") or {}
        if task is None:
            if answers:
                run["judge_reasons"]["rejudge"] = f"no task file named {run['task']}: left as it was"
            graded.append(run)
            continue
        asked = run.get("asked") or {}
        changed = [
            key
            for key, now in (("probe", task.probe_question), ("needle", task.needle_question))
            if key in answers and key in asked and asked[key] != now
        ]
        if changed:
            run["judge_reasons"]["rejudge"] = f"the {' and '.join(changed)} question changed since: left as it was"
            graded.append(run)
            continue
        if any(str(e).startswith(UNVERDICTED) for e in run.get("errors") or []):
            # Saved before 2026-11-09: after Liber refused the plan, the questions went out unedited
            # (condition A). They say nothing about this condition, so no verdict.
            run["success"] = run["needle_kept"] = None
            run["judge_reasons"]["rejudge"] = "Liber's plan wasn't sent: no verdict (answers kept for reference)"
            graded.append(run)
            continue
        probe, needle = answers.get("probe"), answers.get("needle")
        if probe is not None:
            run["success"], run["judge_reasons"]["probe"] = await judge(
                api, judge_model, task.probe_question, task.probe_rubric, probe
            )
        if needle is not None:
            run["needle_kept"], run["judge_reasons"]["needle"] = await judge(
                api, judge_model, task.needle_question, needle_rubric(task), needle
            )
        run["rejudged"] = probe is not None or needle is not None
        if run.get("binding_errors"):
            run["success"] = False if probe is None else run["success"]
            run["needle_kept"] = False if needle is None else run["needle_kept"]
        graded.append(run)
    return graded


async def rejudge_file(api: MessagesApi, path: Path, tasks_dir: Path, judge_model: str, now: datetime) -> str:
    """Rejudge a history file in place, keeping the original beside it as <name>.json.bak.

    In place, not as a new file: it is the same run, and a second file would show up as a second
    point in the report's trends. The .bak suffix keeps the copy out of those trends.
    """
    data = read_history(path)
    tasks = {task.name: task for task in load_tasks(tasks_dir)}
    runs = await rejudge(api, tasks, judge_model, data["runs"])
    return write_rejudged(path, data, runs, judge_model, now)


def read_history(path: Path) -> dict[str, Any]:
    """A Tier 2 history file; ValueError if it isn't one (JSON, with a list of runs)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} isn't JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        raise ValueError(f"{path} isn't a Tier 2 history file (no list of runs)")
    return data


def write_rejudged(
    path: Path, data: dict[str, Any], runs: list[dict[str, Any]], judge_model: str, now: datetime
) -> str:
    """Back up the original, write the regraded run and its report, and say how the verdicts came out.

    The backup is written once: a second rejudge must not replace the true original with the first
    rejudge's output.
    """
    backup = path.with_name(path.name + ".bak")
    if not backup.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    meta = {key: value for key, value in data.items() if key not in ("runs", "date")}
    meta["rejudged"] = [
        *meta.get("rejudged", []),
        {
            "at": now.isoformat(timespec="seconds"),
            "judge_model": judge_model,
            "previous_judge_model": data.get("judge_model"),
        },
    ]
    meta["judge_model"] = judge_model
    tier2_report.save_history(path, path.with_suffix(".md"), runs, meta)
    regraded = [run for run in runs if run["rejudged"]]
    verdicts = [run[key] for run in regraded for key in ("success", "needle_kept")]
    passed, failed = verdicts.count(True), verdicts.count(False)
    skipped = sum(1 for run in runs if "rejudge" in run["judge_reasons"])
    message = f"rejudged {path.resolve()}: {len(regraded)} runs, {passed} pass, {failed} fail"
    message += f", {len(verdicts) - passed - failed} missing"
    return message + (f"; {skipped} runs left as they were (see judge_reasons)" if skipped else "")


async def grade(
    api: MessagesApi, task: Task, settings: Settings, run: ConditionRun, probe: str | None, needle: str | None
) -> None:
    """The judge's verdicts. A binding break is a failed answer: in real use the request would have failed."""
    run.asked = {"probe": task.probe_question, "needle": task.needle_question}
    if probe is not None:
        run.answers["probe"] = probe
        run.success, run.judge_reasons["probe"] = await judge(
            api, settings.judge_model, task.probe_question, task.probe_rubric, probe
        )
    if needle is not None:
        run.answers["needle"] = needle
        run.needle_kept, run.judge_reasons["needle"] = await judge(
            api, settings.judge_model, task.needle_question, needle_rubric(task), needle
        )
    if run.binding_errors:
        run.success = False if probe is None else run.success
        run.needle_kept = False if needle is None else run.needle_kept


async def run_task_repeat(
    api: MessagesApi, worker_client: ModelClient, task: Task, settings: Settings, repeat: int
) -> list[ConditionRun]:
    history, starts, after_setup = await setup_history(api, task, settings)
    problems = workspace_problems(task, after_setup)
    with_text = thinking_with_text(history)
    through = starts[task.compact_through_turn + 1] - 1  # the message before the next turn starts
    runs = []
    with tempfile.TemporaryDirectory(prefix="liber-tier2-") as temp:
        directory = Path(temp)
        summaries = await worker_summaries(task, settings, history, worker_client, directory)
        for condition in task.conditions():
            run = ConditionRun(
                task.name,
                condition,
                repeat,
                task.reasoning_dependent,
                thinking_with_text=with_text,
                tools_in_questions=task.questions_may_use_tools,
            )
            if problems:  # the file and the conversation disagree: nothing fair to ask
                run.errors += problems
                runs.append(run)
                continue
            if task.reasoning_dependent and with_text == 0:
                run.errors.append("no thinking text came back in setup: C can't differ from B on this repeat")
            workspace = dict(after_setup)  # each condition reads and writes its own copy
            if condition == "A":
                send = direct_sender(api, base_request(task, settings.model))
                probe, needle = await questions(send, list(history), task, run, workspace)
            elif condition in ("B", "C", "E"):
                probe, needle = await run_liber(
                    api, task, settings, history, through, summaries, run, directory, workspace
                )
            else:
                probe, needle = await run_client_compaction(api, task, settings, history, run, workspace)
            await grade(api, task, settings, run, probe, needle)
            run.cost = input_cost(run.usage + run.overhead, settings.model, settings.pricing)
            runs.append(run)
    return runs


async def run_eval(
    api: MessagesApi,
    worker_client: ModelClient,
    tasks: list[Task],
    settings: Settings,
    progress: Callable[[str], None] = print,
    save: Callable[[list[ConditionRun]], None] | None = None,
) -> list[ConditionRun]:
    """Every task, every repeat. A repeat that fails is recorded (as errors on its four runs) and the
    eval goes on; `save` is called after each repeat with everything so far."""
    runs: list[ConditionRun] = []
    for task in tasks:
        for repeat in range(1, settings.repeats + 1):
            progress(f"{task.name}: repeat {repeat}/{settings.repeats}")
            try:
                runs += await run_task_repeat(api, worker_client, task, settings, repeat)
            except Exception as exc:
                problem = f"repeat failed: {type(exc).__name__}: {str(exc)[:200]}"
                progress(f"  {problem}")
                failed = [
                    ConditionRun(task.name, c, repeat, task.reasoning_dependent, errors=[problem])
                    for c in task.conditions()
                ]
                runs += failed
            if save is not None:
                save(runs)
    return runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m liber.evals.tier2", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--live", action="store_true", help="really call the API (it costs money)")
    parser.add_argument("--model", default="claude-sonnet-5-5", help="the model the conversations run on")
    parser.add_argument("--judge-model", default="claude-opus-5-5")
    parser.add_argument("--summary-model", default=DEFAULT_SUMMARY_MODEL)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--tasks", type=Path, default=TASKS_DIR)
    parser.add_argument("--out", type=Path, default=HISTORY_DIR)
    parser.add_argument(
        "--rejudge",
        type=Path,
        metavar="HISTORY_JSON",
        help="only grade that run's saved answers again (judge calls only)",
    )
    args = parser.parse_args(argv)
    if not args.live:
        print("Tier 2 calls real models and costs money. Run it with --live (never in CI).")
        return 2
    if os.environ.get("CI"):
        print("Tier 2 never runs in CI (CLAUDE.md hard rule 7).")
        return 2
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("Set ANTHROPIC_API_KEY first.")
        return 2
    if args.repeats < 1:
        print("--repeats must be at least 1")
        return 2
    if args.rejudge is not None:
        if not args.rejudge.is_file():
            print(f"No such file: {args.rejudge}")
            return 2
        try:
            read_history(args.rejudge)  # fail before any API client exists
        except ValueError as exc:
            print(exc)
            return 2

        async def regrade() -> str:
            api = HttpMessagesApi(key)
            try:
                return await rejudge_file(api, args.rejudge, args.tasks, args.judge_model, datetime.now(UTC))
            finally:
                await api.aclose()

        print(asyncio.run(regrade()))
        return 0
    from liber.worker.client import AnthropicModelClient  # the real worker model, only when live

    settings = Settings(args.model, args.judge_model, args.summary_model, args.repeats)
    tasks = load_tasks(args.tasks)
    meta = {
        "model": args.model,
        "judge_model": args.judge_model,
        "summary_model": args.summary_model,
        "repeats": args.repeats,
    }
    json_path, report_path = tier2_report.history_paths(args.out, datetime.now(UTC))

    def save(runs: list[ConditionRun]) -> None:
        tier2_report.save_history(json_path, report_path, [asdict(run) for run in runs], meta)

    async def go() -> None:
        api = HttpMessagesApi(key)
        worker_client = AnthropicModelClient(key, "https://api.anthropic.com")
        try:
            await run_eval(api, worker_client, tasks, settings, save=save)
        finally:
            await api.aclose()
            await worker_client.aclose()

    asyncio.run(go())
    print(f"results: {json_path.resolve()}\nreport:  {report_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
