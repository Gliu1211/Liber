"""Tier 1 offline evals: the merge gate's numbers (ARCHITECTURE.md §12 Tier 1).

    uv run python -m liber.evals.tier1 --out evals/results/latest.json     (or: make eval)

Every session fixture is replayed as the requests a client would send (liber/evals/replay.py).
Each request goes through Liber twice, offline:

- **The model functions directly** (parse, render with the plan the request path would apply,
  validate), to check that the rendered request is valid, reproduces the client's request when
  nothing is planned, and comes out byte-identical when rendered again.
- **The real request path** (pipeline.prepare, with a fresh SQLite database), to measure how
  often Liber steps aside and how long steps 2-5 of §3 take.

Results go to `--out` in the format scripts/eval_gate.py compares with evals/baseline.json, and a
per-request trace goes next to it (`latest.trace.jsonl`) for diagnosing a failing metric.
Everything except timings is deterministic: two runs on the same code give the same files.

Metrics (definitions in docs/decisions/0006-tier1-metric-definitions.md):
    validity_rate        rendered requests that pass the validator           (must stay 1.0)
    round_trip_identity  empty plan reproduces the request, key order too    (must stay 1.0)
    fallback_rate        requests the pipeline forwarded untouched instead   (must not rise)
    token_reduction      1 - estimated tokens sent / tokens received         (0 until Session 4)
    prefix_stability     placeholder 1.0 until compaction exists (Session 4)
    render_determinism   rendering the same request twice gives the same bytes (ADR 0004 A2)
    p95_overhead_ms      §3 steps 2-5 on the request path; its tolerance is the budget left
"""

import argparse
import asyncio
import hashlib
import json
import statistics
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from liber import pipeline
from liber.config import DEFAULT_WAL_CHECKPOINT_SECONDS
from liber.evals.metrics import Metric, p95, ratio, write_results, write_trace
from liber.evals.replay import ReplayedRequest, SessionFixture, load_sessions, replay
from liber.model.anthropic import RecentRequests, parse, render
from liber.model.items import ExchangeSummary, Kind, Operation, OperationKind, Plan
from liber.plan import versions
from liber.plan.apply import exchange_starts, summarize_operation
from liber.session.store import Store, checkpointing

OVERHEAD_BUDGET_MS = 20.0  # §3 / ADR 0011: request-path p95 budget for steps 2-5
DEFAULT_SESSIONS = Path("tests/fixtures/sessions")
DEFAULT_OUT = Path("evals/results/latest.json")

# Sent once before measuring, so one-off startup costs (thread pool, first query) aren't timed.
WARMUP_BODY = b'{"model": "warm-up", "system": "warm-up", "messages": [{"role": "user", "content": "warm-up"}]}'


# The request path is timed this many times, each on a fresh database; the reported p95 is the
# median of the runs' p95s, so one unlucky run (a scheduler hiccup) doesn't move the gate.
TIMING_RUNS = 3


class EvalError(Exception):
    """The run measured nothing (no fixtures, or a session with no requests): not a pass."""


@dataclass
class RenderCheck:
    """What the model functions did with one request, outside the request path."""

    round_trip: bool = False
    deterministic: bool = False
    rendered_sha256: str | None = None  # fingerprint of the bytes Liber would send if it edited
    tokens_before: int = 0
    error: str | None = None


@dataclass
class Tier1Result:
    metrics: dict[str, Metric]
    trace: list[dict[str, Any]]
    summary: dict[str, Any]


def check_render(body: bytes, plan: Plan) -> RenderCheck:
    """Round trip, determinism and token estimate for one request, from scratch each time."""
    check = RenderCheck()
    try:
        reference = json.loads(body)  # an independent copy to compare against
        parsed = parse(json.loads(body))
        check.tokens_before = sum(item.tokens for item in parsed.items)
        # Round trip is always about the empty plan, even once real plans exist: "no edits" must
        # give back the client's request, equal as data *and* in key order.
        unedited = render(parsed, Plan())
        check.round_trip = unedited == reference and json.dumps(unedited) == json.dumps(reference)
        # Render twice from scratch: the bytes must not vary (ADR 0004 A2). The fingerprint goes in
        # the trace, so two runs in separate processes can be compared too (a restart of Liber).
        first = pipeline.encode_request(render(parse(json.loads(body)), plan))
        second = pipeline.encode_request(render(parse(json.loads(body)), plan))
        check.deterministic = first == second
        check.rendered_sha256 = hashlib.sha256(first).hexdigest()
    except Exception as exc:
        check.error = pipeline.describe_failure(exc)
    return check


@dataclass(frozen=True)
class StandardPlan:
    """A fixture session's standard plan (docs/decisions/0010).

    Version 1 summarizes the first exchange and offloads the large tool results of the next one.
    Version 2 adds a summary of the second exchange: a minor compaction, which appends a frozen
    block and must leave everything before it byte-identical (prefix_stability).
    """

    first: tuple[Operation, ...]
    second: tuple[Operation, ...]

    def version(self, number: int | None) -> Plan:
        operations = {1: self.first, 2: self.first + self.second}.get(number or 0, ())
        return Plan(operations=operations)


LARGE_TOOL_RESULT_TOKENS = 200


def fixture_summaries(count: int) -> tuple[ExchangeSummary, ...]:
    """Stand-ins for the worker's summaries of `count` exchanges: fixed text, no model (Tier 1 is offline).

    About the length of a real one (a sentence or two), so token_reduction stays representative.
    """
    return tuple(
        ExchangeSummary(
            topic=f"fixture exchange {number}",
            summary="The user asked for a change; the assistant read the relevant files, made the edit "
            "and ran the tests, which passed. Nothing is left open from this exchange.",
        )
        for number in range(1, count + 1)
    )


def standard_plan(session: SessionFixture) -> StandardPlan | None:
    """The standard plan for `session`, from its final request (item ids don't change as it grows).

    None for a session with fewer than three exchanges: there would be nothing live left.
    """
    items = parse(session.request).items
    starts = exchange_starts(session.request["messages"])
    if len(starts) < 3:
        return None
    first = [summarize_operation(items, 0, starts[1] - 1, block_number=1, summaries=fixture_summaries(1))]
    large = tuple(
        item.id
        for item in items
        if item.kind is Kind.TOOL_RESULT
        and item.tokens >= LARGE_TOOL_RESULT_TOKENS
        and starts[1] <= item.msg_index < starts[2]
    )
    if large:
        first.append(Operation(kind=OperationKind.OFFLOAD, item_ids=large, note="large tool result"))
    second = (summarize_operation(items, starts[1], starts[2] - 1, block_number=2, summaries=fixture_summaries(1)),)
    return StandardPlan(tuple(first), second)


async def run_request_path(
    requests: list[ReplayedRequest], plans: dict[str, StandardPlan] | None = None
) -> list[pipeline.Prepared]:
    """Send every request through pipeline.prepare against a fresh, throwaway database.

    Set up as the app sets it up (liber/app.py), as if freshly started: an empty recent-requests
    cache (ADR 0007) and the background WAL checkpoints (ADR 0008). With `plans`, each fixture
    session gets its standard plan the way a user would make it: version 1 is created and
    requested after the session's first request, version 2 once version 1 is in use; the request
    path applies each at a clean turn (§6, ADR 0004 A1). Plan commands run between requests, so
    they aren't timed.
    """
    plans = plans or {}
    liber_session: dict[str, str] = {}  # fixture session -> its Liber session (its first request's)
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "liber.db")
        await store.create_tables()
        recent = RecentRequests()
        results = []
        try:
            async with checkpointing(store, DEFAULT_WAL_CHECKPOINT_SECONDS):
                await pipeline.prepare(store, WARMUP_BODY, recent)
                for request in requests:
                    result = await pipeline.prepare(store, request.body, recent)
                    results.append(result)
                    plan = plans.get(request.session)
                    if plan is None or result.session_id is None:
                        continue
                    session_id = liber_session.setdefault(request.session, result.session_id)
                    if session_id != result.session_id:
                        continue  # e.g. the client compacted its own history: a new session, no plan
                    state = await versions.load_state(store, session_id)
                    newest = state.pending or state.applied
                    if newest.number == 0:
                        await versions.create(store, session_id, plan.first)
                        await versions.apply(store, session_id)
                    elif newest.number == 1 and state.applied.number == 1:
                        await versions.create(store, session_id, plan.second)
                        await versions.apply(store, session_id)
        finally:
            await store.close()
    return results


def keeps_frozen_prefix(body: bytes, plan: StandardPlan) -> bool:
    """Does rendering with version 2 keep version 1's bytes up to the end of its frozen blocks?

    Those bytes (system, tools, earlier frozen blocks) are what the provider's prefix cache has
    already seen; a minor compaction may only append after them (D5).
    """
    before = pipeline.encode_request(render(parse(json.loads(body)), plan.version(1)))
    after = pipeline.encode_request(render(parse(json.loads(body)), plan.version(2)))
    last_frozen = pipeline.encode_request({"type": "text", "text": plan.first[0].summary})
    end = before.index(last_frozen) + len(last_frozen)
    return after.startswith(before[:end])


def thinking_blocks_in(body: bytes) -> int:
    request = json.loads(body)
    return sum(
        1
        for message in request["messages"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") in ("thinking", "redacted_thinking")
    )


def tokens_sent(result: pipeline.Prepared, tokens_before: int) -> int:
    """Estimated tokens in what actually went upstream: the client's own request unless Liber
    sent a rendered one. A render that failed validation was never sent, so it saves nothing."""
    if result.outcome != "rendered":
        return tokens_before
    return sum(item.tokens for item in parse(json.loads(result.body)).items)


async def evaluate(sessions_dir: Path, budget_ms: float = OVERHEAD_BUDGET_MS) -> Tier1Result:
    sessions = load_sessions(sessions_dir)
    if not sessions:
        raise EvalError(f"no session fixtures in {sessions_dir}")
    requests: list[ReplayedRequest] = []
    for session in sessions:
        replayed = replay(session)
        if not replayed:
            raise EvalError(f"session {session.name} replays to no requests")
        requests += replayed

    plans = {session.name: plan for session in sessions if (plan := standard_plan(session)) is not None}
    runs = [await run_request_path(requests, plans) for _ in range(TIMING_RUNS)]
    prepared = runs[0]  # outcomes don't depend on timing; the first run is the one traced
    # Each request is checked with the plan version the request path rendered it with.
    used = [
        plans[r.session].version(p.plan_version) if r.session in plans else Plan()
        for r, p in zip(requests, prepared, strict=True)
    ]
    checks = [check_render(request.body, plan) for request, plan in zip(requests, used, strict=True)]
    # Minor compactions: every request rendered with version 2, against version 1 (D5).
    compacted = [
        keeps_frozen_prefix(request.body, plans[request.session])
        for request, result in zip(requests, prepared, strict=True)
        if result.plan_version == 2
    ]

    trace: list[dict[str, Any]] = []
    for request, check, result in zip(requests, checks, prepared, strict=True):
        trace.append(
            {
                "session": request.session,
                "label": request.label,
                "index": request.index,
                "messages": len(json.loads(request.body)["messages"]),
                "plan_version": result.plan_version,
                "edit_skipped": result.edit_skipped,
                "betas": list(result.betas),
                # Thinking blocks in what was sent (ADR 0012: none on the request a version takes effect).
                "thinking_sent": thinking_blocks_in(result.body),
                "outcome": result.outcome,
                "liber_error": result.error,
                "known_messages": result.known_messages,
                "new_messages": result.new_messages,
                # Validity is judged on what the request path itself rendered.
                "valid": result.rendered is not None and not result.violations,
                "violations": [str(v) for v in result.violations],
                "round_trip": check.round_trip,
                "deterministic": check.deterministic,
                "rendered_sha256": check.rendered_sha256,
                "tokens_before": check.tokens_before,
                "tokens_after": tokens_sent(result, check.tokens_before),
                "check_error": check.error,
                "overhead_ms": result.overhead_ms,
            }
        )

    total = len(requests)
    tokens_before = sum(record["tokens_before"] for record in trace)
    tokens_after = sum(record["tokens_after"] for record in trace)
    run_p95s = [p95([result.overhead_ms or 0.0 for result in run]) for run in runs]
    overhead = statistics.median(run_p95s)
    metrics = {
        "validity_rate": Metric(
            ratio(sum(record["valid"] for record in trace), total),
            "higher",
            0.0,
            "Share of requests whose rendering on the request path passes the validator (§5).",
        ),
        "round_trip_identity": Metric(
            ratio(sum(check.round_trip for check in checks), total),
            "higher",
            0.0,
            "Share of requests an empty plan reproduces exactly, key order included.",
        ),
        "fallback_rate": Metric(
            round(sum(result.outcome == "fallback" for result in prepared) / total, 6),
            "lower",
            0.0,
            "Share of requests the pipeline forwarded untouched because something failed.",
        ),
        "token_reduction": Metric(
            round(1 - tokens_after / tokens_before, 6) if tokens_before else 0.0,
            "higher",
            0.02,
            "1 - estimated input tokens actually sent / tokens the client sent, over all requests.",
        ),
        "prefix_stability": Metric(
            # A run with standard plans but no minor compaction measured nothing: that counts as 0,
            # so the gate fails, rather than ratio()'s 1.0 for an empty set.
            ratio(sum(compacted), len(compacted)) if compacted or not plans else 0.0,
            "higher",
            0.0,
            "Share of requests rendered after a minor compaction (plan version 2) whose bytes up to "
            "the end of version 1's frozen blocks are unchanged (D5).",
        ),
        "render_determinism": Metric(
            ratio(sum(check.deterministic for check in checks), total),
            "higher",
            0.0,
            "Share of requests that render to identical bytes when rendered twice (ADR 0004 A2).",
        ),
        "p95_overhead_ms": Metric(
            round(overhead, 3),
            "lower",
            # "Budget left": the gate fails once p95 rises by more than the tolerance, i.e. exactly
            # when it passes the budget. Negative if the baseline itself was over budget, so the
            # gate still demands getting back under it.
            round(budget_ms - overhead, 3),
            f"p95 of §3 steps 2-5 on the request path, in ms; median of {TIMING_RUNS} runs (budget {budget_ms:g} ms).",
        ),
    }
    summary = {
        "sessions": [session.name for session in sessions],
        "requests": total,
        "overhead_budget_ms": budget_ms,
        # Not gated, but worth a look: p95 hides single slow requests.
        "max_overhead_ms": max(result.overhead_ms or 0.0 for run in runs for result in run),
        "plans": "standard plan per fixture session with at least 3 exchanges (docs/decisions/0010)",
        "minor_compactions_checked": len(compacted),
    }
    return Tier1Result(metrics=metrics, trace=trace, summary=summary)


def trace_path(results_path: Path) -> Path:
    return results_path.with_name(results_path.stem + ".trace.jsonl")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m liber.evals.tier1", description="Tier 1 offline evals")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="results file (default %(default)s)")
    parser.add_argument("--sessions", type=Path, default=DEFAULT_SESSIONS, help="session fixtures directory")
    parser.add_argument("--budget-ms", type=float, default=OVERHEAD_BUDGET_MS, help="request-path p95 budget")
    args = parser.parse_args(argv)

    try:
        result = asyncio.run(evaluate(args.sessions, args.budget_ms))
    except EvalError as exc:
        print(f"tier1: FAILED: {exc}")
        return 2  # an empty run must never look like a perfect one
    write_results(args.out, result.metrics, result.summary)
    write_trace(trace_path(args.out), result.trace)

    print(f"tier1: {result.summary['requests']} requests from {len(result.summary['sessions'])} sessions")
    for name, metric in result.metrics.items():
        print(f"  {name:20s} {metric.value:g}")
    print(f"  results: {args.out}  trace: {trace_path(args.out)}")
    return 0  # pass/fail on the numbers is the gate's job (scripts/eval_gate.py)


if __name__ == "__main__":
    raise SystemExit(main())
