"""Shared test helpers: a fake upstream API and a way to run the proxy on a real socket.

Everything here is offline (CLAUDE.md hard rule 7). The fake upstream is plugged into the
proxy's httpx client through `httpx.MockTransport`, so no request ever leaves the process.
"""

import asyncio
import hashlib
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI

from liber.app import wait_for_pending_writes
from liber.config import DEFAULT_SUMMARY_MODEL
from liber.model.anthropic import ParsedRequest
from liber.session.store import RequestRecord, Store
from liber.worker.run import Worker

FIXTURES = Path(__file__).parent / "fixtures"

STREAM_FIXTURE = FIXTURES / "stream_thinking_text_tool.sse"
STREAM_EXPECTED = FIXTURES / "stream_thinking_text_tool.expected.json"
STREAM_ERROR_FIXTURE = FIXTURES / "stream_error_midway.sse"
NONSTREAM_FIXTURE = FIXTURES / "message_nonstream.json"

# Fake credentials. Tests assert these strings never reach the database or the logs.
SECRET_API_KEY = "sk-ant-api03-SECRET-test-key-never-store-me"
SECRET_BEARER = "Bearer SECRET-oauth-token-never-store-me"

MESSAGES_BODY: dict[str, Any] = {
    "model": "claude-opus-5-5",
    "max_tokens": 1024,
    "stream": True,
    "messages": [{"role": "user", "content": "Run the tests"}],
}

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class FakeUpstream:
    """Stands in for api.anthropic.com.

    A test sets `handler` to decide what the upstream returns; every request the proxy sends
    is kept in `requests` so the test can check what was forwarded.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Handler = self._default_handler

    async def _default_handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        response = await self.handler(request)
        if response.is_stream_consumed:
            # httpx.Response(content=b"...") is born already read, but a real network transport
            # always returns an unread stream. Re-wrap it so the proxy sees what it would see
            # in production. (Content-Length is kept, so no chunked encoding is added.)
            response = httpx.Response(
                response.status_code, headers=response.headers, content=stream_of([response.content])
            )
        return response

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    @property
    def last_request(self) -> httpx.Request:
        return self.requests[-1]


def chunked(data: bytes, sizes: Iterable[int]) -> list[bytes]:
    """Split `data` into pieces of the given sizes (cycling), to mimic arbitrary network chunking."""
    pieces: list[bytes] = []
    sizes = list(sizes)
    position, i = 0, 0
    while position < len(data):
        size = sizes[i % len(sizes)]
        pieces.append(data[position : position + size])
        position += size
        i += 1
    return pieces


async def stream_of(pieces: Iterable[bytes]) -> AsyncIterator[bytes]:
    """An async byte stream; giving one to httpx.Response makes the fake upstream stream it."""
    for piece in pieces:
        yield piece


def sse_response(body: bytes, sizes: Iterable[int] = (64,), headers: dict[str, str] | None = None) -> httpx.Response:
    """A streamed `text/event-stream` response, delivered in chunks of the given sizes."""
    all_headers = {"content-type": "text/event-stream; charset=utf-8", "request-id": "req_test_1"}
    all_headers.update(headers or {})
    return httpx.Response(200, headers=all_headers, content=stream_of(chunked(body, sizes)))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


async def recorded(app: FastAPI) -> list[RequestRecord]:
    """All rows in the `requests` table, after any pending background writes have finished."""
    await wait_for_pending_writes(app)
    records: list[RequestRecord] = await app.state.store.list_requests()
    return records


@asynccontextmanager
async def running_server(app: FastAPI) -> AsyncIterator[str]:
    """Run the app under a real uvicorn server on a free localhost port; yields its base URL.

    Needed for tests about *timing* (chunks arriving before the upstream finishes, client
    disconnects): httpx's in-process ASGITransport buffers the whole response, so it can only
    check the final bytes.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "uvicorn did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        sock.close()


class FakeModel:
    """Stands in for the worker's model (worker/client.ModelClient); nothing leaves the process.

    Token counts are a quarter of the JSON length. Summaries are made from a hash of the prompt,
    so the same exchange always gets the same one. `failures` are raised, in order, before any
    answer; `reply` replaces the structured answer. Every call is kept for the test to inspect.
    """

    def __init__(self) -> None:
        self.counts: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
        self.calls: list[dict[str, Any]] = []
        self.failures: list[Exception] = []
        self.reply: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self.slept: list[float] = []  # retry delays, when a test's worker records them here

    def summary_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if "summary" in call["schema"]["properties"]]

    def notes_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if "notes" in call["schema"]["properties"]]

    async def count_tokens(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        self.counts.append((model, messages, tools))
        if self.failures:
            raise self.failures.pop(0)
        return len(json.dumps(messages)) // 4 + len(json.dumps(tools)) // 4

    async def structured(
        self, model: str, system: str, prompt: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        call = {"model": model, "system": system, "prompt": prompt, "schema": schema, "max_tokens": max_tokens}
        self.calls.append(call)
        if self.failures:
            raise self.failures.pop(0)
        if self.reply is not None:
            return self.reply(call)
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        if "notes" in schema["properties"]:
            return {"notes": [f"decided {digest} because of the reasoning"]}
        return {"topic": f"topic {digest}", "summary": f"summary {digest}"}


async def summarize_now(
    store: Store, session_id: str, parsed: ParsedRequest, carry_forward: bool = False, model: FakeModel | None = None
) -> FakeModel:
    """Run the worker over one request's history right away, as it would in the background."""
    model = model or FakeModel()
    worker = Worker(store, model, DEFAULT_SUMMARY_MODEL, carry_forward_reasoning=carry_forward)
    worker.submit(session_id, parsed)
    await worker.process(worker.waiting.pop(session_id))
    return model
