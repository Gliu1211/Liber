"""Benchmark the request path (ARCHITECTURE.md §3 steps 2-5) beyond what the Tier 1 gate checks.

    uv run python scripts/bench_request_path.py [--runs 10]

`pipeline.prepare` is timed the way the app runs it (liber/app.py): a fresh database, the
recent-requests cache (ADR 0007) and the background WAL checkpoints (ADR 0008). Only each
request's timing is kept; like the app, the decoded request is let go once it's done. (The
Tier 1 harness keeps every result for its trace, so its max can include a slow garbage
collection of all of them.)

1. **Tier 1 replay:** every session fixture, `--runs` times. Prints the p95 and the slowest
   request (max) of each run. The gate uses the median of three p95s; max is what p95 hides.
2. **A ~1 MB conversation:** a synthetic Claude Code-style session of 401 messages (~1 MB on its
   last turn), replayed turn by turn. Prints p95, mean and max over its last 20 turns, each a
   ~1 MB request that adds two messages. Real Claude Code contexts are this size and larger, so
   this shows how the request path scales, which the ~90 KB fixtures can't.

Offline and deterministic apart from timings. Not part of `make check`: it reports, it doesn't gate.
"""

import argparse
import asyncio
import hashlib
import statistics
import tempfile
from pathlib import Path

from liber import pipeline
from liber.config import DEFAULT_WAL_CHECKPOINT_SECONDS
from liber.evals.metrics import p95
from liber.evals.replay import SessionFixture, load_sessions, replay
from liber.evals.tier1 import DEFAULT_SESSIONS, WARMUP_BODY
from liber.model.anthropic import RecentRequests
from liber.session.store import Store, checkpointing

LAST_TURNS = 20  # turns of the ~1 MB session that are measured (all of them ~1 MB)


async def time_request_path(bodies: list[bytes]) -> list[float]:
    """pipeline.prepare's overhead_ms for each body, in order, set up as the app sets it up."""
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "liber.db")
        await store.create_tables()
        recent = RecentRequests()
        try:
            async with checkpointing(store, DEFAULT_WAL_CHECKPOINT_SECONDS):
                await pipeline.prepare(store, WARMUP_BODY, recent)
                return [(await pipeline.prepare(store, body, recent)).overhead_ms or 0.0 for body in bodies]
        finally:
            await store.close()


def large_session(exchanges: int = 40, tool_rounds: int = 4, file_lines: int = 84) -> SessionFixture:
    """A Claude Code-shaped session: per exchange, a task, tool rounds with thinking, an answer.

    Same shape as tests/fixtures/requests/build_long_session.py, scaled up: the defaults give
    401 messages and ~1 MB, with a ~11 KB system prompt and 15 tool definitions. Every tool call
    and tool result holds a number or a boolean.
    """
    messages: list[dict[str, object]] = []
    tool_number = 0
    for exchange in range(exchanges):
        messages.append(
            {"role": "user", "content": f"Task {exchange}: refactor module_{exchange}.py and run its tests."}
        )
        for round_number in range(tool_rounds):
            tool_number += 1
            tool_id = f"toolu_bench_{tool_number:04d}"
            thinking = f"Exchange {exchange}, round {round_number}: inspect the next file. " * 4
            code = "".join(
                f"def function_{tool_number}_{i}(value):  # line {i}\n    return value * {i} + {tool_number}\n"
                for i in range(file_lines)
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": thinking,
                            "signature": "Eq" + hashlib.sha256(f"signature-{tool_number}".encode()).hexdigest(),
                        },
                        {"type": "text", "text": f"Reading file {tool_number}."},
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "read_file",
                            # Numbers, as in Claude Code's tool calls: these messages take parse()'s
                            # slower type-checking comparison (ADR 0007), so it's measured too.
                            "input": {
                                "path": f"src/module_{exchange}/part_{round_number}.py",
                                "offset": 0,
                                "limit": 200,
                            },
                        },
                    ],
                }
            )
            result = {"type": "tool_result", "tool_use_id": tool_id, "content": code, "is_error": False}
            messages.append({"role": "user", "content": [result]})
        messages.append({"role": "assistant", "content": [{"type": "text", "text": f"Done with task {exchange}."}]})
    messages.append({"role": "user", "content": "Great. Now summarise every change."})
    request = {
        "model": "claude-opus-5-5",
        "max_tokens": 32000,
        "stream": True,
        "thinking": {"type": "adaptive"},
        "system": [{"type": "text", "text": "You are a coding agent working in a Python repository. " * 200}],
        "tools": [
            {
                "name": f"tool_{i}",
                "description": "A tool. " * 40,
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            }
            for i in range(15)
        ],
        "messages": messages,
    }
    return SessionFixture(name="bench_1mb", description="synthetic ~1 MB session", request=request, cache_marker="last")


async def main(runs: int) -> None:
    fixtures = [request.body for session in load_sessions(DEFAULT_SESSIONS) for request in replay(session)]
    print(f"Tier 1 replay: {len(fixtures)} requests, {runs} runs (fresh database each)")
    run_p95s, run_maxes = [], []
    for _ in range(runs):
        timings = await time_request_path(fixtures)
        run_p95s.append(p95(timings))
        run_maxes.append(max(timings))
    print(f"  p95 per run (ms): {', '.join(f'{v:.2f}' for v in run_p95s)}  -> median {statistics.median(run_p95s):.2f}")
    print(
        f"  max per run (ms): {', '.join(f'{v:.1f}' for v in run_maxes)}  -> median {statistics.median(run_maxes):.1f}"
    )

    large = [request.body for request in replay(large_session())]
    print(
        f"~1 MB session: {len(large)} requests, last {LAST_TURNS} measured ({len(large[-1]) / 1024:.0f} KB last turn)"
    )
    for run in range(3):
        timings = (await time_request_path(large))[-LAST_TURNS:]
        print(
            f"  run {run + 1}: p95 {p95(timings):.2f} ms, mean {statistics.mean(timings):.2f} ms, "
            f"max {max(timings):.2f} ms"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark the request path")
    parser.add_argument("--runs", type=int, default=10, help="Tier 1 replay runs (default %(default)s)")
    asyncio.run(main(parser.parse_args().runs))
