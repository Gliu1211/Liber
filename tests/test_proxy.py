"""Acceptance tests for the transparent passthrough proxy (SESSION_PROMPTS.md, Session 1).

The client talks to Liber; Liber talks to a fake upstream. The proxy must be invisible: the
client gets exactly what the upstream sent, and the upstream gets exactly what the client sent.
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest

from liber.app import create_app
from liber.config import Settings
from liber.proxy.forward import relay
from liber.session.store import RequestRecord
from tests.helpers import (
    MESSAGES_BODY,
    NONSTREAM_FIXTURE,
    STREAM_ERROR_FIXTURE,
    STREAM_EXPECTED,
    STREAM_FIXTURE,
    FakeUpstream,
    chunked,
    load_json,
    recorded,
    sse_response,
    stream_of,
)

Records = Callable[[], Awaitable[list[RequestRecord]]]

CLIENT_HEADERS = {
    "x-api-key": "sk-ant-test-not-a-real-key",
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}


# --- Streaming ------------------------------------------------------------------------------


@pytest.mark.parametrize("sizes", [(1,), (7,), (64,), (3, 50, 1, 200), (100_000,)])
async def test_streaming_passthrough_is_byte_faithful(
    client: httpx.AsyncClient, upstream: FakeUpstream, sizes: tuple[int, ...]
) -> None:
    body = STREAM_FIXTURE.read_bytes()

    async def handler(request: httpx.Request) -> httpx.Response:
        # Chunk sizes like 1 and 7 split lines, JSON and multi-byte UTF-8 characters.
        return sse_response(body, sizes=sizes, headers={"anthropic-organization-id": "org_x"})

    upstream.handler = handler
    response = await client.post("/v1/messages", json=MESSAGES_BODY, headers=CLIENT_HEADERS)

    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["request-id"] == "req_test_1"
    assert response.headers["anthropic-organization-id"] == "org_x"


async def test_streamed_reply_is_reassembled_and_usage_recorded(
    client: httpx.AsyncClient, upstream: FakeUpstream, records: Records
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(STREAM_FIXTURE.read_bytes(), sizes=(5,))

    upstream.handler = handler
    await client.post("/v1/messages", json=MESSAGES_BODY, headers=CLIENT_HEADERS)

    [row] = await records()
    assert row.status_code == 200
    assert row.stream is True
    assert row.model == "claude-opus-5-5"
    assert row.stop_reason == "tool_use"
    assert row.upstream_request_id == "req_test_1"
    # Usage: input + cache numbers come from message_start, output from the final message_delta.
    assert row.input_tokens == 12
    assert row.output_tokens == 87
    assert row.cache_read_input_tokens == 20480
    assert row.cache_creation_input_tokens == 1500
    assert row.error is None
    assert row.response_body is not None
    assert json.loads(row.response_body) == load_json(STREAM_EXPECTED)
    assert row.request_body is not None
    assert json.loads(row.request_body) == MESSAGES_BODY
    assert row.latency_ms is not None and row.latency_ms >= 0
    assert row.duration_ms is not None and row.duration_ms >= row.latency_ms


async def test_error_event_mid_stream_is_passed_through_and_recorded(
    client: httpx.AsyncClient, upstream: FakeUpstream, records: Records
) -> None:
    body = STREAM_ERROR_FIXTURE.read_bytes()

    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(body)

    upstream.handler = handler
    response = await client.post("/v1/messages", json=MESSAGES_BODY, headers=CLIENT_HEADERS)

    assert response.status_code == 200
    assert response.content == body
    [row] = await records()
    assert row.input_tokens == 30
    assert row.error is not None and "overloaded_error" in row.error


async def test_unparseable_stream_still_reaches_client_unchanged(
    client: httpx.AsyncClient, upstream: FakeUpstream, records: Records
) -> None:
    # Liber's parsing is best-effort: garbage must still be relayed byte for byte.
    body = b"event: message_start\ndata: {this is not json\n\n\xff\xfe binary junk\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(body, sizes=(3,))

    upstream.handler = handler
    response = await client.post("/v1/messages", json=MESSAGES_BODY, headers=CLIENT_HEADERS)

    assert response.status_code == 200
    assert response.content == body
    [row] = await records()
    assert row.status_code == 200
    assert row.error is not None and "parse" in row.error


async def test_upstream_failure_mid_stream_is_not_hidden(
    client: httpx.AsyncClient, upstream: FakeUpstream, records: Records
) -> None:
    async def broken_body() -> AsyncIterator[bytes]:
        yield b"event: ping\ndata: {}\n\n"
        raise httpx.ReadError("connection reset by upstream")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=broken_body())

    upstream.handler = handler
    # The upstream broke after the response started, so the client must see a broken stream
    # (not a clean, silently truncated one).
    with pytest.raises(httpx.ReadError):
        await client.post("/v1/messages", json=MESSAGES_BODY, headers=CLIENT_HEADERS)

    [row] = await records()
    assert row.error is not None and "ReadError" in row.error


# --- Non-streaming ----------------------------------------------------------------------------


async def test_non_streaming_request(client: httpx.AsyncClient, upstream: FakeUpstream, records: Records) -> None:
    body = NONSTREAM_FIXTURE.read_bytes()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json", "request-id": "req_2"}, content=body)

    upstream.handler = handler
    request_body = {**MESSAGES_BODY, "stream": False}
    response = await client.post("/v1/messages", json=request_body, headers=CLIENT_HEADERS)

    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-length"] == str(len(body))
    assert response.headers["content-type"] == "application/json"

    [row] = await records()
    assert row.stream is False
    assert row.stop_reason == "end_turn"
    assert (row.input_tokens, row.output_tokens) == (25, 9)
    assert (row.cache_read_input_tokens, row.cache_creation_input_tokens) == (2048, 0)
    assert row.response_body is not None
    assert json.loads(row.response_body) == load_json(NONSTREAM_FIXTURE)


@pytest.mark.parametrize(
    ("status", "error_type", "extra_headers"),
    [
        (400, "invalid_request_error", {}),
        (401, "authentication_error", {}),
        (429, "rate_limit_error", {"retry-after": "17", "x-should-retry": "true"}),
        (500, "api_error", {"x-should-retry": "true"}),
        (529, "overloaded_error", {}),
    ],
)
async def test_upstream_errors_keep_status_body_and_headers(
    client: httpx.AsyncClient,
    upstream: FakeUpstream,
    records: Records,
    status: int,
    error_type: str,
    extra_headers: dict[str, str],
) -> None:
    body = json.dumps(
        {"type": "error", "error": {"type": error_type, "message": "nope"}, "request_id": "req_e"}
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-type": "application/json", **extra_headers}
        return httpx.Response(status, headers=headers, content=body)

    upstream.handler = handler
    response = await client.post("/v1/messages", json=MESSAGES_BODY, headers=CLIENT_HEADERS)

    assert response.status_code == status
    assert response.content == body
    for name, value in extra_headers.items():
        assert response.headers[name] == value

    [row] = await records()
    assert row.status_code == status
    assert row.error is not None and error_type in row.error


@pytest.mark.parametrize(
    ("exception", "expected_status"),
    [(httpx.ConnectError("refused"), 502), (httpx.ConnectTimeout("slow"), 504)],
)
async def test_unreachable_upstream_returns_an_api_style_error(
    client: httpx.AsyncClient,
    upstream: FakeUpstream,
    records: Records,
    exception: httpx.HTTPError,
    expected_status: int,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    upstream.handler = handler
    response = await client.post("/v1/messages", json=MESSAGES_BODY, headers=CLIENT_HEADERS)

    # Clients (and the Anthropic SDKs) understand this shape and retry 5xx errors.
    assert response.status_code == expected_status
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "api_error"

    [row] = await records()
    assert row.status_code == expected_status
    assert row.error is not None and type(exception).__name__ in row.error


async def test_request_body_and_path_reach_upstream_unchanged(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    # Odd key order, unicode and spacing must survive: Liber forwards the raw bytes.
    raw = (
        '{"stream":false,  "model":"claude-opus-5-5","max_tokens":5,"messages":[{"role":"user","content":"héllo"}]}'
    ).encode()

    await client.post("/v1/messages?beta=true", content=raw, headers=CLIENT_HEADERS)

    sent = upstream.last_request
    assert sent.method == "POST"
    assert sent.url == httpx.URL("https://upstream.test/v1/messages?beta=true")
    assert sent.content == raw


# --- Other paths ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/v1/models?limit=2&after_id=abc", None),
        ("POST", "/v1/messages/count_tokens", b'{"model":"claude-opus-5-5","messages":[]}'),
        ("DELETE", "/v1/files/file_123", None),
        ("GET", "/v1/messages", None),  # only POST /v1/messages is special
        ("GET", "/docs", None),  # FastAPI's built-in docs pages are disabled
        ("PATCH", "/some/future/endpoint", b"payload"),
    ],
)
async def test_other_paths_are_passed_through(
    client: httpx.AsyncClient,
    upstream: FakeUpstream,
    records: Records,
    method: str,
    path: str,
    body: bytes | None,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            203,
            headers={"content-type": "application/json", "x-upstream": "yes"},
            content=b'{"x":1}',
        )

    upstream.handler = handler
    response = await client.request(method, path, content=body, headers=CLIENT_HEADERS)

    assert response.status_code == 203
    assert response.content == b'{"x":1}'
    assert response.headers["x-upstream"] == "yes"
    sent = upstream.last_request
    assert sent.method == method
    assert sent.url == httpx.URL("https://upstream.test" + path)
    assert sent.content == (body or b"")
    # Only POST /v1/messages is recorded in Session 1.
    assert await records() == []


# --- Headers ----------------------------------------------------------------------------------


async def test_client_headers_pass_through_and_hop_by_hop_are_dropped(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    headers = [
        ("x-api-key", "sk-ant-test-key"),
        ("authorization", "Bearer test-token"),
        ("anthropic-version", "2023-06-01"),
        ("anthropic-beta", "beta-one-2026-01-01"),
        ("anthropic-beta", "beta-two-2026-02-02"),
        ("x-stainless-lang", "python"),
        ("content-type", "application/json"),
        # Hop-by-hop headers describe the client->Liber connection only (RFC 9110 §7.6.1).
        ("connection", "keep-alive, x-only-for-this-hop"),
        ("x-only-for-this-hop", "1"),
        ("te", "trailers"),
        ("proxy-authorization", "Basic aGVsbG8="),
        ("accept-encoding", "gzip, br"),
    ]
    await client.post("/v1/messages", json=MESSAGES_BODY, headers=headers)

    sent = upstream.last_request.headers
    assert sent["x-api-key"] == "sk-ant-test-key"
    assert sent["authorization"] == "Bearer test-token"
    assert sent["anthropic-version"] == "2023-06-01"
    assert sent.get_list("anthropic-beta") == ["beta-one-2026-01-01", "beta-two-2026-02-02"]
    assert sent["x-stainless-lang"] == "python"
    assert sent["host"] == "upstream.test"
    assert "x-only-for-this-hop" not in sent
    assert "te" not in sent
    assert "proxy-authorization" not in sent
    # Liber asks for an uncompressed body so it can read what it relays (docs/decisions/0001).
    assert sent["accept-encoding"] == "identity"


async def test_hop_by_hop_response_headers_are_dropped(client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=[
                ("content-type", "application/json"),
                ("keep-alive", "timeout=5"),
                ("connection", "keep-alive"),
                ("x-custom", "kept"),
                ("set-cookie", "a=1"),
                ("set-cookie", "b=2"),
            ],
            content=stream_of(chunked(b'{"ok":true}', [4])),
        )

    upstream.handler = handler
    response = await client.get("/v1/models", headers=CLIENT_HEADERS)

    assert response.content == b'{"ok":true}'
    assert response.headers["x-custom"] == "kept"
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert "keep-alive" not in response.headers
    # The upstream streamed with chunked encoding; that framing belongs to the upstream hop.
    assert "transfer-encoding" not in response.headers


async def test_only_safe_request_headers_are_recorded(
    client: httpx.AsyncClient, upstream: FakeUpstream, records: Records
) -> None:
    headers = {**CLIENT_HEADERS, "anthropic-beta": "some-beta", "authorization": "Bearer t"}
    await client.post("/v1/messages", json=MESSAGES_BODY, headers=headers)

    [row] = await records()
    assert row.request_headers is not None
    stored = json.loads(row.request_headers)
    assert stored["anthropic-version"] == "2023-06-01"
    assert stored["anthropic-beta"] == "some-beta"
    assert "x-api-key" not in stored
    assert "authorization" not in stored


async def test_recording_bodies_can_be_disabled(settings: Settings, upstream: FakeUpstream) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(STREAM_FIXTURE.read_bytes())

    upstream.handler = handler
    app = create_app(settings.model_copy(update={"record_bodies": False}), upstream_transport=upstream.transport)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        await client.post("/v1/messages", json=MESSAGES_BODY, headers=CLIENT_HEADERS)
        [row] = await recorded(app)

    assert row.request_body is None
    assert row.response_body is None
    # Usage is metadata, not content, so it is still recorded.
    assert row.output_tokens == 87


# --- Robustness (review findings) -------------------------------------------------------------


async def test_non_ascii_header_values_pass_through_byte_for_byte(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    # Header values are bytes on the wire; Liber must not decode/re-encode them (that used to
    # turn valid traffic into a 500).
    request_value = "héllo → wörld".encode()
    response_value = "café ✓".encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=[(b"x-note", response_value)], content=b"{}")

    upstream.handler = handler
    response = await client.post(
        "/v1/messages", json=MESSAGES_BODY, headers=[(b"x-title", request_value), (b"x-api-key", b"k")]
    )

    assert response.status_code == 200
    assert (b"x-title", request_value) in upstream.last_request.headers.raw
    assert (b"x-note", response_value) in response.headers.raw


@pytest.mark.parametrize(
    "raw_body",
    [
        b"[" * 200_000,  # so deeply nested that json.loads raises RecursionError
        b"\xff\xfe not json",
        b"",
    ],
)
async def test_unusual_request_bodies_are_still_forwarded(
    client: httpx.AsyncClient, upstream: FakeUpstream, records: Records, raw_body: bytes
) -> None:
    # Recording is optional, forwarding is not (hard rule 1): if Liber can't make sense of a
    # body, the upstream still gets it and decides.
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error"}})

    upstream.handler = handler
    response = await client.post("/v1/messages", content=raw_body, headers=CLIENT_HEADERS)

    assert response.status_code == 400
    assert upstream.last_request.content == raw_body
    [row] = await records()
    assert row.status_code == 400


async def test_non_network_failure_mid_stream_is_labelled_as_such(
    client: httpx.AsyncClient, upstream: FakeUpstream, records: Records
) -> None:
    async def broken_body() -> AsyncIterator[bytes]:
        yield b"event: ping\ndata: {}\n\n"
        raise RuntimeError("something unexpected")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=broken_body())

    upstream.handler = handler
    with pytest.raises(RuntimeError):
        await client.post("/v1/messages", json=MESSAGES_BODY, headers=CLIENT_HEADERS)

    [row] = await records()
    assert row.error is not None
    assert "RuntimeError" in row.error
    assert "disconnect" not in row.error


async def test_a_failing_chunk_observer_never_breaks_the_stream() -> None:
    # relay() must deliver every byte even if whatever watches the chunks blows up.
    body = STREAM_FIXTURE.read_bytes()
    upstream_response = httpx.Response(200, content=stream_of(chunked(body, [10])))
    endings: list[str | None] = []

    def explode(chunk: bytes) -> None:
        raise ValueError("observer bug")

    received = b""
    async for chunk in relay(upstream_response, on_chunk=explode, on_end=endings.append):
        received += chunk

    assert received == body
    assert endings == [None]


async def test_data_directory_is_private(app: object, settings: Settings) -> None:
    # It holds conversation bodies (§11): only the owner may read it.
    assert settings.data_dir.stat().st_mode & 0o777 == 0o700
