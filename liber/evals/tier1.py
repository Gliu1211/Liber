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
from liber.config import Settings
from liber.evals.metrics import Metric, p95, ratio, write_results, write_trace
from liber.evals.replay import ReplayedRequest, load_sessions, replay
from liber.model.anthropic import RecentRequests, parse, render
from liber.model.items import Plan
from liber.session.store import Store, checkpoint_regularly

OVERHEAD_BUDGET_MS = 10.0  # §3: steps 2-5 must add only a few milliseconds
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


async def run_request_path(requests: list[ReplayedRequest]) -> list[pipeline.Prepared]:
    """Send every request through pipeline.prepare against a fresh, throwaway database.

    Set up as the app sets it up (liber/app.py), as if freshly started: an empty recent-requests
    cache (ADR 0007) and the background WAL checkpoints (ADR 0008).
    """
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "liber.db")
        await store.create_tables()
        recent = RecentRequests()
        checkpoints = asyncio.create_task(checkpoint_regularly(store, Settings().wal_checkpoint_seconds))
        try:
            await pipeline.prepare(store, WARMUP_BODY, recent)
            return [await pipeline.prepare(store, request.body, recent) for request in requests]
        finally:
            checkpoints.cancel()
            await store.close()


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

    plan = Plan()  # the plan the request path applies: always empty until Session 4
    checks = [check_render(request.body, plan) for request in requests]
    runs = [await run_request_path(requests) for _ in range(TIMING_RUNS)]
    prepared = runs[0]  # outcomes don't depend on timing; the first run is the one traced

    trace: list[dict[str, Any]] = []
    for request, check, result in zip(requests, checks, prepared, strict=True):
        trace.append(
            {
                "session": request.session,
                "label": request.label,
                "index": request.index,
                "messages": len(json.loads(request.body)["messages"]),
                "plan_version": None,  # no plans until Session 4
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
            1.0,
            "higher",
            0.0,
            "Placeholder until Session 4: share of requests whose bytes up to the frozen/live "
            "boundary are unchanged after a minor compaction.",
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
        "plan": "empty (plans arrive in Session 4)",
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
