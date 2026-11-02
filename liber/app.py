"""The FastAPI application: routes and lifespan (ARCHITECTURE.md §3, §13).

Request path in Session 1 (a transparent proxy, no context editing yet):

    POST /v1/messages
      1. read the raw body                       (§3 step 1)
      2. forward it upstream unchanged           (§3 step 6)
      3. relay the response chunk by chunk, while ReplyCapture watches a copy
      4. when the body ends, record the exchange (§3 step 7) in a background task

    any other method/path
      forward and relay unchanged, no recording

Steps 2-5 of §3 (identify session, parse, render with the edit plan, validate) arrive in
Session 2 and slot in between 1 and 2 above.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import structlog
from fastapi import FastAPI, Request, Response

from liber.config import Settings
from liber.log import configure_logging
from liber.proxy.forward import (
    create_upstream_client,
    gateway_error,
    open_upstream,
    relay,
    streaming_response,
)
from liber.proxy.sse import CapturedReply, ReplyCapture
from liber.session.store import RequestRecord, Store, recordable_headers

log = structlog.get_logger(__name__)

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_app(settings: Settings | None = None, upstream_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """Build the app. Tests pass `upstream_transport` to swap the real network for a fake."""
    settings = settings or Settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: open the database and the shared upstream client. (The background worker
        # from §9 will also start here.)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = Store(settings.db_path)
        await store.create_tables()
        app.state.store = store
        app.state.client = create_upstream_client(settings, upstream_transport)
        try:
            yield
        finally:
            # Shutdown: finish recording in-flight exchanges before closing the database.
            await wait_for_pending_writes(app)
            await app.state.client.aclose()
            await store.close()

    # docs/redoc/openapi are switched off: those paths belong to the upstream API, and the proxy
    # must not answer anything itself.
    app = FastAPI(title="Liber", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.pending_writes = set()
    # Order matters: the specific route first, then the catch-all for everything else.
    app.add_api_route("/v1/messages", proxy_messages, methods=["POST"])
    app.add_api_route("/{path:path}", proxy_other, methods=ALL_METHODS)
    return app


async def proxy_messages(request: Request) -> Response:
    """POST /v1/messages: forward, stream the reply back, and record the exchange."""
    app = request.app
    settings: Settings = app.state.settings
    started = time.perf_counter()
    body = await request.body()
    record = new_record(request, body, settings.record_bodies)

    try:
        upstream = await open_upstream(app.state.client, request, body)
    except httpx.HTTPError as exc:
        log.warning("upstream_unreachable", path=request.url.path, error=type(exc).__name__)
        response = gateway_error(exc)
        record.status_code = response.status_code
        record.error = f"upstream unreachable: {type(exc).__name__}: {exc}"
        record.duration_ms = elapsed_ms(started)
        spawn_write(app, record)
        return response

    record.status_code = upstream.status_code
    record.upstream_request_id = upstream.headers.get("request-id")
    record.latency_ms = elapsed_ms(started)
    capture = ReplyCapture(upstream.headers.get("content-type", ""), upstream.headers.get("content-encoding"))

    def finish(relay_error: str | None) -> None:
        # Called by relay() once the body has ended (or stopped early). Recording is best
        # effort: a failure here is logged and must never affect the client's response.
        try:
            record.duration_ms = elapsed_ms(started)
            fill_from_reply(record, capture.result(), settings.record_bodies)
            problems = [p for p in (relay_error, record.error) if p]
            record.error = "; ".join(problems) or None
            spawn_write(app, record)
        except Exception:
            log.exception("recording_failed", path=request.url.path)

    return streaming_response(upstream, relay(upstream, on_chunk=capture.feed, on_end=finish))


async def proxy_other(request: Request) -> Response:
    """Every other endpoint (token counting, model listing, ...) is passed through unchanged."""
    body = await request.body()
    try:
        upstream = await open_upstream(request.app.state.client, request, body)
    except httpx.HTTPError as exc:
        log.warning("upstream_unreachable", path=request.url.path, error=type(exc).__name__)
        return gateway_error(exc)
    return streaming_response(upstream, relay(upstream))


# --- Recording helpers ------------------------------------------------------------------------


def new_record(request: Request, body: bytes, keep_body: bool) -> RequestRecord:
    """Start a `requests` row with what is known before forwarding."""
    model, stream = request_summary(body)
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RequestRecord(
        created_at=datetime.now(UTC),
        method=request.method,
        path=path,
        model=model,
        stream=stream,
        request_headers=recordable_headers(request.headers.items()),
        request_body=body.decode("utf-8", errors="replace") if keep_body else None,
    )


def request_summary(body: bytes) -> tuple[str | None, bool | None]:
    """The model name and stream flag from a request body; Nones if it isn't usable JSON."""
    try:
        parsed = json.loads(body)
    except ValueError:  # includes invalid UTF-8
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    model = parsed.get("model")
    stream = parsed.get("stream", False)  # the API's default is non-streaming
    return (
        model if isinstance(model, str) else None,
        stream if isinstance(stream, bool) else None,
    )


def fill_from_reply(record: RequestRecord, reply: CapturedReply, keep_body: bool) -> None:
    record.stop_reason = reply.stop_reason
    record.input_tokens = reply.usage.input_tokens
    record.output_tokens = reply.usage.output_tokens
    record.cache_read_input_tokens = reply.usage.cache_read_input_tokens
    record.cache_creation_input_tokens = reply.usage.cache_creation_input_tokens
    record.response_body = reply.body_text if keep_body else None
    record.error = reply.problem


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def spawn_write(app: FastAPI, record: RequestRecord) -> None:
    """Save `record` in a background task, so the client's response never waits on the database."""
    task = asyncio.create_task(_write(app, record))
    # Keep a reference until the task is done (asyncio only keeps weak references to tasks),
    # and so shutdown and tests can wait for pending writes.
    app.state.pending_writes.add(task)
    task.add_done_callback(app.state.pending_writes.discard)


async def _write(app: FastAPI, record: RequestRecord) -> None:
    try:
        await app.state.store.add_request(record)
    except Exception:
        log.exception("recording_failed", path=record.path)
        return
    log.info(
        "messages_request",
        status=record.status_code,
        model=record.model,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        cache_read=record.cache_read_input_tokens,
        cache_write=record.cache_creation_input_tokens,
        latency_ms=record.latency_ms,
        duration_ms=record.duration_ms,
        error=record.error,
    )


async def wait_for_pending_writes(app: FastAPI) -> None:
    """Wait for every background database write to finish (used at shutdown and by tests)."""
    while app.state.pending_writes:
        await asyncio.gather(*app.state.pending_writes)
