"""Live end-to-end check against the real API: owner-run, costs a few cents, never in CI.

    ANTHROPIC_API_KEY=... uv run python -m liber.evals.live [--model claude-sonnet-5-5]

Why it exists: every other test uses a fake upstream. This is the first check that Liber works on
real traffic, and the real test of ADR 0012 (a compacted request with thinking must not be
rejected, and the API must have nothing to drop).

What it does, with a throwaway data directory so your real ~/.liber is untouched:
1. Starts `liber serve` on a spare port, with the background worker on (same key).
2. Holds a six-turn conversation through it with adaptive thinking: one turn uses a tool, some
   turns stream, and turn 1 plants a fact ("the needle") for later.
3. Waits for the worker's summaries, then `liber plan create --summarize-through` (turns 1-4,
   including a ~2,000-token tool result) and `liber plan apply`.
4. Sends two more turns. The first asks for the needle; before sending it, the API counts that
   same request as the client sent it, so "context got smaller" compares like with like.
5. Reads Liber's own records and reports PASS/FAIL per check (see `evaluate`).

The decision logic (`summary_end`, `evaluate`) is plain functions, tested offline in
tests/test_live_check.py.
"""

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PORT = 8499
NEEDLE_PROMPT = (
    "Remember this for later: the project codename is BLUE HERON and the deadline is March 3. Reply in one sentence."
)
NEEDLE_WORDS = ("blue heron", "march 3")
PROMPTS = [
    NEEDLE_PROMPT,
    "What's the weather in Paris over the next three days? Use the get_weather tool.",  # tool turn: tool pairing
    "Give me five name ideas for a coffee shop, each with a two-sentence pitch.",
    "In about 150 words, explain what a hash chain is and where git uses one.",
    "Write a one-line Python function that reverses a string.",
    "Briefly: what is 17 * 23?",
]
AFTER_PROMPTS = [
    "What was the project codename and the deadline I told you at the very start?",
    "Thanks. Last one: what is 2 + 2?",
]
STREAMED_TURNS = {2, 4, 6}  # 0-based turn numbers sent with streaming, like Claude Code does
# Same tools on every request: they're part of the session's identity (D7: h_0 = H(system || tools)).
TOOLS = [
    {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    }
]


def forecast() -> str:
    """A long tool result (~2,000 tokens), like the logs and file contents real sessions carry.

    Compaction only pays off when there is something sizeable to summarize; a one-line tool result
    would be shorter than its summary, and Liber now refuses such plans (owner, 2026-11-06).
    """
    hours = [
        {
            "time": f"day {day} {hour:02d}:00",
            "temp_c": 12 + (hour % 9),
            "rain_mm": round((hour * 7 % 5) / 4, 2),
            "wind_kmh": 8 + hour % 13,
        }
        for day in (1, 2, 3)
        for hour in range(24)
    ]
    return json.dumps({"city": "Paris", "hourly": hours})


def summary_end(messages: list[dict[str, Any]], turn_starts: list[int], through_turn: int) -> int:
    """Message index a summary of turns 0..through_turn ends at.

    `plan create` requires the message after the summary to be a user message that starts an
    exchange (not a tool_result), so the summary ends just before the next turn's first message.
    """
    nxt = turn_starts[through_turn + 1]
    first = messages[nxt]
    if first["role"] != "user" or not isinstance(first["content"], str):
        raise ValueError(f"message {nxt} doesn't start an exchange")
    return nxt - 1


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def evaluate(
    rows: list[dict[str, Any]], applied_at: int, needle_answer: str, worker_rows: int, original_tokens: int | None
) -> list[Check]:
    """Turn Liber's request records into PASS/FAIL checks.

    rows: the session's /v1/messages records in order; applied_at: index of the first row sent
    after `liber plan apply`; original_tokens: the API's token count for that first request as
    the client sent it, i.e. without Liber (the same request, so the comparison is fair).
    """
    after = rows[applied_at:]
    rendered = [r for r in after if r["liber_outcome"] == "rendered" and r["plan_version"] is not None]
    checks = [
        Check(
            "no request rejected",
            all(r["status_code"] == 200 for r in rows),
            ", ".join(f"#{i}:{r['status_code']}" for i, r in enumerate(rows) if r["status_code"] != 200) or "all 200",
        ),
        Check(
            "no fallbacks",
            not any(r["liber_outcome"] == "fallback" for r in rows),
            "; ".join(str(r["liber_error"]) for r in rows if r["liber_outcome"] == "fallback") or "none",
        ),
        Check(
            "compaction sent",
            bool(rendered),
            f"{len(rendered)} rendered request(s) after apply"
            + ("" if rendered else f"; edit_skipped: {[r['edit_skipped'] for r in after]}"),
        ),
    ]
    dropped = [r for r in rendered if _thinking_dropped(r["input_transformations"])]
    checks.append(
        Check(
            "API dropped no thinking (ADR 0012)",
            bool(rendered) and not dropped,
            "nothing dropped"
            if not dropped
            else f"dropped on {len(dropped)} request(s): Liber's stripping missed something",
        )
    )
    sent = total_input(rendered[0]) if rendered else None
    detail = f"same request: {original_tokens} tokens without Liber, {sent} with Liber"
    smaller = False
    if original_tokens and sent is not None:
        smaller = sent < original_tokens
        detail += f" ({1 - sent / original_tokens:.0%} smaller)" if smaller else ""
    checks.append(Check("context got smaller", smaller, detail))
    checks.append(Check("worker wrote summaries", worker_rows > 0, f"{worker_rows} summary row(s)"))
    kept = all(word in needle_answer.lower() for word in NEEDLE_WORDS)
    checks.append(Check("summary kept the planted fact", kept, needle_answer.strip()[:160]))
    return checks


def total_input(row: dict[str, Any]) -> int | None:
    """All input tokens of a request: usage's input_tokens leaves out cached ones."""
    if row["input_tokens"] is None:
        return None
    cached = int(row.get("cache_read_input_tokens") or 0) + int(row.get("cache_creation_input_tokens") or 0)
    return int(row["input_tokens"]) + cached


def _thinking_dropped(raw: str | None) -> bool:
    if not raw:
        return False
    entries = json.loads(raw)
    return any(isinstance(e, dict) and e.get("type") == "thinking_dropped" for e in entries)


# --- Everything below talks to the real API and a real proxy process. ---


def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"liber serve didn't start on port {port}; see the log in the data directory")


def _send(client: Any, model: str, messages: list[dict[str, Any]], stream: bool) -> Any:
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 2000,
        "thinking": {"type": "adaptive"},
        "tools": TOOLS,
        "messages": messages,
    }
    if stream:
        with client.messages.stream(**kwargs) as s:
            return s.get_final_message()
    return client.messages.create(**kwargs)


def _turn(client: Any, model: str, messages: list[dict[str, Any]], prompt: str, stream: bool) -> str:
    """One user turn, including a tool round trip if the model asks for one. Returns the reply text."""
    messages.append({"role": "user", "content": prompt})
    while True:
        reply = _send(client, model, messages, stream)
        # Echo the content exactly as received, thinking blocks included (preserved thinking).
        messages.append({"role": "assistant", "content": reply.content})
        if reply.stop_reason != "tool_use":
            return "".join(b.text for b in reply.content if b.type == "text")
        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": forecast()}
            for b in reply.content
            if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})


def _liber(data_dir: Path, *args: str) -> str:
    env = {**os.environ, "LIBER_DATA_DIR": str(data_dir)}
    out = subprocess.run([sys.executable, "-m", "liber.cli", *args], env=env, capture_output=True, text=True)
    return (out.stdout + out.stderr).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--model", default="claude-sonnet-5-5")
    args = parser.parse_args()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("Set ANTHROPIC_API_KEY first. This check costs a few cents of API usage.")
        return 2
    import anthropic  # imported here so the offline tests don't need it configured

    data_dir = Path(tempfile.mkdtemp(prefix="liber-live-"))
    log = (data_dir / "proxy.log").open("w")
    env = {**os.environ, "LIBER_DATA_DIR": str(data_dir), "LIBER_WORKER_API_KEY": key}
    proxy = subprocess.Popen(
        [sys.executable, "-m", "liber.cli", "serve", "--port", str(PORT)], env=env, stdout=log, stderr=subprocess.STDOUT
    )
    try:
        _wait_for_port(PORT)
        client = anthropic.Anthropic(api_key=key, base_url=f"http://127.0.0.1:{PORT}")
        messages: list[dict[str, Any]] = []
        turn_starts: list[int] = []
        for n, prompt in enumerate(PROMPTS):
            turn_starts.append(len(messages))
            _turn(client, args.model, messages, prompt, stream=n in STREAMED_TURNS)
            print(f"turn {n + 1}/{len(PROMPTS)} done")

        db = sqlite3.connect(data_dir / "liber.db")
        db.row_factory = sqlite3.Row
        session = db.execute("SELECT id FROM sessions ORDER BY created_at LIMIT 1").fetchone()["id"]
        last = summary_end(messages, [*turn_starts, len(messages)], through_turn=3)
        print(f"waiting for the worker's summaries, then summarizing messages 0-{last} ...")
        deadline = time.monotonic() + 180
        while True:
            out = _liber(data_dir, "plan", "create", "--session", session, "--summarize-through", str(last))
            if "not ready" not in out or time.monotonic() > deadline:
                break
            time.sleep(5)
        print(f"  {out}")
        print(f"  {_liber(data_dir, 'plan', 'apply', '--session', session)}")
        applied_at = db.execute("SELECT COUNT(*) FROM requests WHERE session_id = ?", (session,)).fetchone()[0]

        # What the first compacted request would have cost without Liber: the same request, counted
        # by the API directly (not through the proxy).
        direct: Any = anthropic.Anthropic(api_key=key)  # Any: the SDK's typed params, like `client` above
        original = direct.messages.count_tokens(
            model=args.model,
            thinking={"type": "adaptive"},
            tools=TOOLS,
            messages=[*messages, {"role": "user", "content": AFTER_PROMPTS[0]}],
        ).input_tokens
        needle_answer = _turn(client, args.model, messages, AFTER_PROMPTS[0], stream=True)
        _turn(client, args.model, messages, AFTER_PROMPTS[1], stream=False)

        rows = [
            dict(r)
            for r in db.execute(
                "SELECT status_code, liber_outcome, liber_error, plan_version, edit_skipped, input_tokens,"
                " cache_read_input_tokens, cache_creation_input_tokens, input_transformations"
                " FROM requests WHERE session_id = ? ORDER BY id",
                (session,),
            )
        ]
        worker_rows = db.execute("SELECT COUNT(*) FROM exchange_summaries").fetchone()[0]
        checks = evaluate(rows, applied_at, needle_answer, worker_rows, original)
    finally:
        proxy.terminate()
        proxy.wait(timeout=10)
        log.close()

    print()
    for c in checks:
        print(f"{'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}")
    print(f"\nRecords and proxy log kept in {data_dir}")
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
