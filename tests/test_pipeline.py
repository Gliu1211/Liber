"""The request path, §3 steps 2-5 (liber/pipeline.py): identify, register, render, validate.

Hard rule 1 is the centre of these tests: whatever goes wrong inside Liber, the upstream gets
the client's original bytes.
"""

import json
import statistics
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from liber import pipeline
from liber.model.anthropic import RecentRequests
from liber.session import chain
from liber.session.store import Store
from tests.helpers import FIXTURES, FakeUpstream, recorded

LONG_SESSION = FIXTURES / "requests" / "long_session_101.json"
TOOL_LOOP = FIXTURES / "requests" / "tool_loop_in_flight.json"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    store = Store(tmp_path / "liber.db")
    await store.create_tables()
    yield store
    await store.close()


def boom(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("injected failure")


async def async_boom(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("injected failure")


# --- Outcomes ---------------------------------------------------------------------------------


async def test_unchanged_request_is_forwarded_byte_for_byte(store: Store) -> None:
    raw = TOOL_LOOP.read_bytes()  # pretty-printed: re-serialising would change the bytes

    prepared = await pipeline.prepare(store, raw)

    assert prepared.outcome == "passthrough"
    assert prepared.body is raw
    assert prepared.error is None and prepared.violations == []
    assert prepared.session_id is not None and prepared.branch_id is not None
    assert (prepared.known_messages, prepared.new_messages) == (0, 5)
    assert prepared.overhead_ms is not None and prepared.overhead_ms >= 0


async def test_non_json_body_falls_back(store: Store) -> None:
    prepared = await pipeline.prepare(store, b"{not json")

    assert prepared.outcome == "fallback"
    assert prepared.body == b"{not json"
    assert prepared.error is not None and "JSONDecodeError" in prepared.error


@pytest.mark.parametrize(
    ("step", "replacement"),
    [("parse", boom), ("identify", async_boom), ("render", boom), ("validate", boom)],
)
async def test_any_exception_forwards_the_original_request(
    store: Store, monkeypatch: pytest.MonkeyPatch, step: str, replacement: Any
) -> None:
    monkeypatch.setattr(pipeline, step, replacement)
    raw = TOOL_LOOP.read_bytes()

    prepared = await pipeline.prepare(store, raw)

    assert prepared.outcome == "fallback"
    assert prepared.body is raw
    assert prepared.error is not None and "RuntimeError" in prepared.error


async def test_validation_failure_forwards_the_original_request(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_render(parsed: Any, plan: Any) -> dict[str, Any]:
        # A render bug that drops the final tool results: the validator must catch it.
        request: dict[str, Any] = json.loads(json.dumps(parsed.original))
        request["messages"][-1]["content"] = [{"type": "text", "text": "oops"}]
        return request

    monkeypatch.setattr(pipeline, "render", broken_render)
    raw = TOOL_LOOP.read_bytes()

    prepared = await pipeline.prepare(store, raw)

    assert prepared.outcome == "fallback"
    assert prepared.body is raw
    assert {v.rule for v in prepared.violations} >= {"tool_pairing", "in_flight_loop"}


async def test_a_client_request_that_breaks_the_rules_is_still_forwarded(store: Store) -> None:
    # Liber's rules are stricter than nothing: if the client's own request fails them, Liber
    # records why and sends it on untouched (the API is the judge).
    raw = json.dumps({"model": "m", "messages": [{"role": "assistant", "content": "hi"}]}).encode()

    prepared = await pipeline.prepare(store, raw)

    assert prepared.outcome == "fallback"
    assert prepared.body is raw
    assert [v.rule for v in prepared.violations] == ["first_is_user"]


async def test_an_empty_plan_that_changes_the_request_is_a_bug_and_falls_back(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lossy_render(parsed: Any, plan: Any) -> dict[str, Any]:
        request: dict[str, Any] = json.loads(json.dumps(parsed.original))
        request.pop("metadata")  # still a valid request, but not the client's request
        return request

    monkeypatch.setattr(pipeline, "render", lossy_render)

    prepared = await pipeline.prepare(store, TOOL_LOOP.read_bytes())

    assert prepared.outcome == "fallback"
    assert prepared.error is not None and "round trip" in prepared.error


# --- Latency budget (§3: steps 2-5 add only a few milliseconds) --------------------------------


def growing_conversation(full: dict[str, Any]) -> list[bytes]:
    """Every request a client would send while having this conversation, in order."""
    bodies = []
    for end in range(1, len(full["messages"]) + 1):
        if full["messages"][end - 1]["role"] == "user":
            bodies.append(json.dumps({**full, "messages": full["messages"][:end]}).encode())
    return bodies


async def test_request_path_overhead_p95_under_10ms(store: Store) -> None:
    full = json.loads(LONG_SESSION.read_text(encoding="utf-8"))
    bodies = growing_conversation(full)
    recent = RecentRequests()  # as the app runs it
    await pipeline.prepare(store, bodies[0], recent)  # warm-up (imports, first DB connection)

    timings = []
    for body in bodies:
        prepared = await pipeline.prepare(store, body, recent)
        assert prepared.outcome == "passthrough", prepared.error or prepared.violations
        assert prepared.overhead_ms is not None
        timings.append(prepared.overhead_ms)
    # Also the worst case: a client that arrives with all 101 messages already in hand.
    cold_body = LONG_SESSION.read_bytes().replace(b"Python repository", b"Go repository")
    cold = await pipeline.prepare(store, cold_body, recent)
    assert cold.overhead_ms is not None
    timings.append(cold.overhead_ms)

    p95 = statistics.quantiles(timings, n=20)[18]
    assert len(bodies) == 51
    assert p95 < 10, f"p95 overhead {p95:.2f} ms over {len(timings)} requests"


# --- Reusing the previous request's work (ADR 0007) -------------------------------------------


async def test_the_next_turn_only_hashes_the_new_messages(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = growing_conversation(json.loads(LONG_SESSION.read_text(encoding="utf-8")))
    recent = RecentRequests()
    await pipeline.prepare(store, bodies[-2], recent)
    hashed: list[Any] = []
    real_canonical_block = chain.canonical_block
    monkeypatch.setattr(chain, "canonical_block", lambda block: hashed.append(block) or real_canonical_block(block))

    prepared = await pipeline.prepare(store, bodies[-1], recent)

    assert prepared.outcome == "passthrough"
    assert (prepared.known_messages, prepared.new_messages) == (99, 2)
    new_messages = json.loads(bodies[-1])["messages"][99:]
    assert hashed == [block for message in new_messages for block in message["content"]]


async def test_the_same_request_twice_hashes_nothing(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = TOOL_LOOP.read_bytes()
    recent = RecentRequests()
    first = await pipeline.prepare(store, raw, recent)
    monkeypatch.setattr(chain, "canonical_block", boom)  # any hashing of a message would now fail

    retry = await pipeline.prepare(store, raw, recent)

    assert retry.outcome == "passthrough" and retry.body is raw
    assert (retry.session_id, retry.known_messages, retry.new_messages) == (first.session_id, 5, 0)


async def test_a_failure_in_the_recent_requests_cache_forwards_the_original(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    recent = RecentRequests()
    monkeypatch.setattr(recent, "find", boom)
    raw = TOOL_LOOP.read_bytes()

    prepared = await pipeline.prepare(store, raw, recent)

    assert prepared.outcome == "fallback"
    assert prepared.body is raw
    assert prepared.error is not None and "RuntimeError" in prepared.error


# --- Through the app --------------------------------------------------------------------------


async def test_outcome_is_recorded_per_request(app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    raw = TOOL_LOOP.read_bytes()

    await client.post("/v1/messages", content=raw, headers={"content-type": "application/json"})
    [row] = await recorded(app)
    [remembered] = app.state.recent_requests.entries  # kept for the conversation's next request

    assert remembered.original == json.loads(raw)

    assert upstream.last_request.content == raw
    assert row.liber_outcome == "passthrough"
    assert row.session_id is not None and row.branch_id is not None
    assert (row.known_messages, row.new_messages) == (0, 5)
    assert row.overhead_ms is not None
    assert row.liber_error is None and row.violations is None


async def test_failures_inside_liber_are_recorded_and_the_original_is_forwarded(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "render", boom)
    raw = TOOL_LOOP.read_bytes()

    response = await client.post("/v1/messages", content=raw, headers={"content-type": "application/json"})
    [row] = await recorded(app)

    assert response.status_code == 200
    assert upstream.last_request.content == raw
    assert row.liber_outcome == "fallback"
    assert row.liber_error is not None and "RuntimeError" in row.liber_error


async def test_violations_are_recorded(app: FastAPI, client: httpx.AsyncClient) -> None:
    raw = json.dumps({"model": "m", "messages": [{"role": "assistant", "content": "hi"}]}).encode()

    await client.post("/v1/messages", content=raw, headers={"content-type": "application/json"})
    [row] = await recorded(app)

    assert row.liber_outcome == "fallback"
    assert row.violations is not None
    [violation] = json.loads(row.violations)
    assert violation.startswith("first_is_user: messages[0]")


# --- Review findings ----------------------------------------------------------------------------


async def test_a_render_that_edits_shared_blocks_in_place_is_caught(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # render() passes the client's block objects through. A bug that edits one of them in place
    # must not be able to fool the validator, so it compares against a separate copy.
    def mutating_render(parsed: Any, plan: Any) -> dict[str, Any]:
        thinking = parsed.original["messages"][1]["content"][0]
        thinking["thinking"] = "rewritten"
        return dict(parsed.original)

    monkeypatch.setattr(pipeline, "render", mutating_render)
    raw = TOOL_LOOP.read_bytes()

    prepared = await pipeline.prepare(store, raw)

    assert prepared.outcome == "fallback"
    assert prepared.body is raw
    assert "thinking_unchanged" in {v.rule for v in prepared.violations}


async def test_errors_never_quote_the_request(store: Store) -> None:
    # The recorded error must not leak conversation text, even with body recording off.
    raw = json.dumps({"model": "m", "messages": [{"role": "my secret token abc", "content": "x"}]}).encode()

    prepared = await pipeline.prepare(store, raw)

    assert prepared.outcome == "fallback"
    assert prepared.error is not None and "secret" not in prepared.error


async def test_unexpected_exception_text_is_not_recorded(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def leaky(*args: Any, **kwargs: Any) -> Any:
        raise KeyError("some conversation text")

    monkeypatch.setattr(pipeline, "render", leaky)

    prepared = await pipeline.prepare(store, TOOL_LOOP.read_bytes())

    assert prepared.error is not None
    assert "KeyError" in prepared.error and "conversation text" not in prepared.error
