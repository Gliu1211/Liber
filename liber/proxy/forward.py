"""Send the client's request upstream and relay the response back unchanged.

ARCHITECTURE.md §3 steps 1 and 6, D12 (credentials pass through untouched).

The proxy must be invisible: the upstream sees the client's method, path, query, body and
headers, and the client sees the upstream's status, headers and body bytes exactly as sent,
chunk by chunk as they arrive. The only headers that change:
- hop-by-hop headers are dropped (they describe one network connection, not the request);
- `host` and `content-length` are recomputed by httpx for the upstream connection;
- `accept-encoding` becomes `identity` (see docs/decisions/0001).

Headers are handled as raw bytes throughout. Decoding them to text and back can fail or change
bytes for non-ASCII values, and the proxy has no reason to interpret them.
"""

from collections.abc import AsyncIterator, Callable, Iterable

import anyio
import httpx
import structlog
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from liber.config import Settings

log = structlog.get_logger(__name__)

Headers = list[tuple[bytes, bytes]]

# RFC 9110 §7.6.1: these apply to one connection (client->Liber or Liber->upstream) and must
# not be forwarded to the next hop.
HOP_BY_HOP_HEADERS = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"trailers",
        b"transfer-encoding",
        b"upgrade",
    }
)

# httpx sets these itself from the upstream URL and the body it sends.
RECOMPUTED_REQUEST_HEADERS = frozenset({b"host", b"content-length"})

CLIENT_DISCONNECTED = "client disconnected before the response finished"


def _without(headers: Iterable[tuple[bytes, bytes]], drop: frozenset[bytes]) -> Headers:
    """Copy headers (keeping duplicates and order) minus `drop` and anything `Connection` names."""
    pairs = list(headers)

    # A sender can mark extra headers as hop-by-hop by listing them in `Connection`.
    named_by_connection = set()
    for name, value in pairs:
        if name.lower() == b"connection":
            for token in value.split(b","):
                named_by_connection.add(token.strip().lower())

    kept = []
    for name, value in pairs:
        lowered = name.lower()
        if lowered not in drop and lowered not in named_by_connection:
            kept.append((lowered, value))
    return kept


def upstream_request_headers(client_headers: Iterable[tuple[bytes, bytes]]) -> Headers:
    """Headers to send upstream: the client's own, including x-api-key/authorization (D12)."""
    headers = _without(client_headers, HOP_BY_HOP_HEADERS | RECOMPUTED_REQUEST_HEADERS | {b"accept-encoding"})
    # Ask for an uncompressed body: then the bytes we relay are also bytes we can read for
    # recording. The client still gets a valid response (docs/decisions/0001).
    headers.append((b"accept-encoding", b"identity"))
    return headers


def client_response_headers(upstream_headers: Iterable[tuple[bytes, bytes]]) -> Headers:
    """Headers to return to the client: the upstream's own, minus hop-by-hop ones.

    `content-length` and `content-encoding` are kept: the body bytes are relayed unmodified,
    so they stay accurate.
    """
    return _without(upstream_headers, HOP_BY_HOP_HEADERS)


def create_upstream_client(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """One shared client for the app's lifetime, so upstream connections are reused.

    `transport` lets tests plug in a fake upstream; in production it is None (real network).
    """
    return httpx.AsyncClient(
        base_url=settings.upstream_base_url,
        timeout=httpx.Timeout(settings.upstream_read_timeout, connect=settings.upstream_connect_timeout),
        transport=transport,
        follow_redirects=False,  # a redirect is the client's business, not the proxy's
    )


async def open_upstream(client: httpx.AsyncClient, request: Request, body: bytes) -> httpx.Response:
    """Forward the request; return as soon as the upstream's status and headers arrive.

    The body has not been read yet (`stream=True`), so it can be relayed chunk by chunk.
    """
    # raw_path keeps the client's exact percent-encoding; query_string is raw bytes too.
    raw_path: bytes = request.scope.get("raw_path") or request.url.path.encode()
    target = raw_path.decode("latin-1")
    if request.url.query:
        target += "?" + request.url.query
    upstream_request = client.build_request(
        request.method,
        target,
        headers=upstream_request_headers(request.headers.raw),
        content=body,
    )
    return await client.send(upstream_request, stream=True)


async def relay(
    upstream: httpx.Response,
    on_chunk: Callable[[bytes], None] | None = None,
    on_end: Callable[[str | None], None] | None = None,
) -> AsyncIterator[bytes]:
    """Yield the upstream body to the client chunk by chunk, as each chunk arrives.

    `on_chunk` sees a copy of every chunk (for recording). `on_end` is called exactly once
    with None if the body was fully relayed, or a short description of what went wrong.
    """
    # Stays like this only if the loop is abandoned without an exception of its own: the
    # generator was closed or cancelled, which happens when the client goes away.
    error: str | None = CLIENT_DISCONNECTED
    try:
        # aiter_raw: the bytes exactly as received, with no decompression or re-encoding.
        async for chunk in upstream.aiter_raw():
            if on_chunk is not None:
                try:
                    on_chunk(chunk)
                except Exception:
                    # A bug in whatever watches the stream must never cost the client its reply.
                    log.exception("chunk_observer_failed")
                    on_chunk = None
            yield chunk
        error = None
    except httpx.HTTPError as exc:
        error = f"upstream stream failed: {describe(exc)}"
        log.warning("upstream_stream_failed", error=describe(exc))
        # Re-raise so the server aborts the connection: the client must see a broken
        # response, not a clean-looking truncated one.
        raise
    except Exception as exc:
        error = f"relay failed: {describe(exc)}"
        log.exception("relay_failed")
        raise
    finally:
        if on_end is not None:
            on_end(error)
        # Shielded because a client disconnect cancels this task, and a cancelled task can't
        # await anything else; without the shield the upstream connection would leak.
        with anyio.CancelScope(shield=True):
            await upstream.aclose()


def streaming_response(upstream: httpx.Response, body: AsyncIterator[bytes]) -> StreamingResponse:
    """A response with the upstream's status and headers whose body is `body` (from `relay`)."""
    response = StreamingResponse(body, status_code=upstream.status_code)
    # Set the raw (name, value) byte pairs directly: keeps repeated headers such as set-cookie
    # and never re-encodes a value.
    response.raw_headers = client_response_headers(upstream.headers.raw)
    return response


def describe(exc: BaseException) -> str:
    """A short description of an error that is safe to log and store.

    Network and timeout errors keep their message ("[Errno 111] Connection refused" is useful).
    Anything else gets only its type name: protocol errors can quote the offending header value,
    and a header value may be a credential (hard rule 2).
    """
    if isinstance(exc, httpx.NetworkError | httpx.TimeoutException):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def gateway_error(exc: Exception) -> JSONResponse:
    """The response when Liber could not forward the request at all.

    Shaped like an Anthropic API error so clients handle it normally; 502/504 are retried by
    the Anthropic SDKs and Claude Code.
    """
    status = 504 if isinstance(exc, httpx.TimeoutException) else 502
    message = f"Liber could not forward the request to the upstream API ({describe(exc)})"
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": "api_error", "message": message}},
    )
