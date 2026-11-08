"""Tier 2 quality evals: what compaction costs in task success, against real models (ARCHITECTURE.md §12).

    ANTHROPIC_API_KEY=... uv run python -m liber.evals.tier2 --live [--repeats 3] [--model ...]

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
from liber.model.items import ExchangeSummary
from liber.plan import commands, cost, versions
from liber.plan.versions import PlanError
from liber.session.store import Store
from liber.worker.client import ModelClient
from liber.worker.run import Job, Worker
from liber.worker.summarize import summarizer_id

TASKS_DIR = Path("evals/tier2_tasks")
HISTORY_DIR = Path("evals/history")
CONDITIONS = tier2_report.CONDITIONS  # ("A", "B", "C", "D")
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
    judge_reasons: dict[str, str] = field(default_factory=dict)


# --- One turn -----------------------------------------------------------------------------------------

Sender = Callable[[list[dict[str, Any]]], Awaitable[ApiResult]]


async def turn(
    send: Sender, messages: list[dict[str, Any]], prompt: str, task: Task, run: ConditionRun | None
) -> str | None:
    """One user turn on `messages` (extended in place), tool round trips included.

    The reply's text, or None if the turn failed: a rejected request, a reply cut off at
    max_tokens, or a model still calling tools after MAX_TOOL_ROUNDS (recorded on `run`).
    """
    messages.append({"role": "user", "content": prompt})
    for _ in range(MAX_TOOL_ROUNDS):
        result = await send(messages)
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
        results = [
            {"type": "tool_result", "tool_use_id": b["id"], "content": task.tool_outputs.get(b["name"], "(no output)")}
            for b in calls
        ]
        messages.append({"role": "user", "content": results})
    if run is not None:
        run.errors.append(f"the model was still calling tools after {MAX_TOOL_ROUNDS} rounds")
    return None


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
        request, betas = with_strict_binding(json.loads(prepared.body))
        return await api.send(request, betas)

    return send


async def setup_history(api: MessagesApi, task: Task, settings: Settings) -> tuple[list[dict[str, Any]], list[int]]:
    """The task's turns with full context: the shared history, and where each turn starts."""
    send = direct_sender(api, base_request(task, settings.model))
    messages: list[dict[str, Any]] = []
    starts = []
    for prompt in task.turns:
        starts.append(len(messages))
        failed = ConditionRun(task.name, "setup", 0)
        if await turn(send, messages, prompt, task, failed) is None:
            raise RuntimeError(f"a setup turn failed: {(failed.binding_errors + failed.errors)[:1]}")
    return messages, starts


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
) -> tuple[str | None, str | None]:
    """B or C: plan from the shared summaries, applied at the probe (a clean turn), then both questions."""
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
        try:
            await commands.create(
                store,
                session,
                through,
                summary_model=settings.summary_model,
                carry_forward_reasoning=run.condition == "C",
            )
            await commands.apply(store, session)
        except PlanError as exc:
            run.errors.append(f"plan refused: {exc}")
        state = await versions.load_state(store, session)
        plan = state.pending.plan if state.pending is not None else state.applied.plan
        run.frozen_blocks = [op.summary for op in plan.operations if op.summary is not None]
        if task.evidence_markers:
            text = "\n".join(run.frozen_blocks).lower()
            run.summary_has_evidence = any(marker.lower() in text for marker in task.evidence_markers)
        answers = await questions(liber_sender(api, base, store, run), list(history), task, run)
        # A request Liber didn't send compacted makes this condition A again: say so in the results.
        unsent = [outcome for outcome in run.liber_outcomes if outcome != "rendered"]
        if unsent:
            run.errors.append(f"Liber didn't send the compaction: {unsent[0]}")
        return answers
    finally:
        await store.close()


async def run_client_compaction(
    api: MessagesApi, task: Task, settings: Settings, history: list[dict[str, Any]], run: ConditionRun
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
    probe = await turn(send, messages, first, task, run)
    needle = await turn(send, messages, task.needle_question, task, run) if probe is not None else None
    return probe, needle


async def questions(
    send: Sender, messages: list[dict[str, Any]], task: Task, run: ConditionRun
) -> tuple[str | None, str | None]:
    """The probe, then (if it got an answer) the needle question in the same conversation."""
    probe = await turn(send, messages, task.probe_question, task, run)
    needle = await turn(send, messages, task.needle_question, task, run) if probe is not None else None
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


async def grade(
    api: MessagesApi, task: Task, settings: Settings, run: ConditionRun, probe: str | None, needle: str | None
) -> None:
    """The judge's verdicts. A binding break is a failed answer: in real use the request would have failed."""
    if probe is not None:
        run.answers["probe"] = probe
        run.success, run.judge_reasons["probe"] = await judge(
            api, settings.judge_model, task.probe_question, task.probe_rubric, probe
        )
    if needle is not None:
        run.answers["needle"] = needle
        rubric = f"Pass only if the answer states this fact correctly: {task.needle_fact}"
        run.needle_kept, run.judge_reasons["needle"] = await judge(
            api, settings.judge_model, task.needle_question, rubric, needle
        )
    if run.binding_errors:
        run.success = False if probe is None else run.success
        run.needle_kept = False if needle is None else run.needle_kept


async def run_task_repeat(
    api: MessagesApi, worker_client: ModelClient, task: Task, settings: Settings, repeat: int
) -> list[ConditionRun]:
    history, starts = await setup_history(api, task, settings)
    with_text = thinking_with_text(history)
    through = starts[task.compact_through_turn + 1] - 1  # the message before the next turn starts
    runs = []
    with tempfile.TemporaryDirectory(prefix="liber-tier2-") as temp:
        directory = Path(temp)
        summaries = await worker_summaries(task, settings, history, worker_client, directory)
        for condition in CONDITIONS:
            run = ConditionRun(task.name, condition, repeat, task.reasoning_dependent, thinking_with_text=with_text)
            if task.reasoning_dependent and with_text == 0:
                run.errors.append("no thinking text came back in setup: C can't differ from B on this repeat")
            if condition == "A":
                send = direct_sender(api, base_request(task, settings.model))
                probe, needle = await questions(send, list(history), task, run)
            elif condition in ("B", "C"):
                probe, needle = await run_liber(api, task, settings, history, through, summaries, run, directory)
            else:
                probe, needle = await run_client_compaction(api, task, settings, history, run)
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
                    ConditionRun(task.name, c, repeat, task.reasoning_dependent, errors=[problem]) for c in CONDITIONS
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
    print(f"results: {json_path}\nreport:  {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
