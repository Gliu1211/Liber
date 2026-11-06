"""The FastAPI application: routes and lifespan (ARCHITECTURE.md §3, §13).

Request path:

    POST /v1/messages
      1. read the raw body                       (§3 step 1)
      2. pipeline.prepare(): parse, identify the conversation, render with the applied plan,
         validate, and choose the bytes to send  (§3 steps 2-5)
      3. hand the identified request to the background worker, without waiting (§9)
      4. forward those bytes upstream            (§3 step 6)
      5. relay the response chunk by chunk, while ReplyCapture watches a copy
      6. when the body ends, record the exchange (§3 step 7) in a background task

    any other method/path
      forward and relay unchanged, no recording

Hard rule 1: recording is optional, forwarding is not. Every recording step is guarded so a
failure in it is logged and the request still goes through.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from fastapi import FastAPI, Request, Response

from liber.cache.blobs import BlobStore
from liber.cache.content import keep_originals
from liber.config import Settings
from liber.log import configure_logging
from liber.model.anthropic import RecentRequests
from liber.pipeline import Prepared, prepare
from liber.plan.policy import PlanPolicy
from liber.proxy.forward import (
    create_upstream_client,
    describe,
    gateway_error,
    open_upstream,
    relay,
    streaming_response,
)
from liber.proxy.sse import CapturedReply, ReplyCapture
from liber.session.store import RequestRecord, Store, checkpointing, recordable_headers
from liber.worker.client import AnthropicModelClient, ModelClient
from liber.worker.run import Worker, running

log = structlog.get_logger(__name__)

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_app(
    settings: Settings | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    model_client: ModelClient | None = None,
) -> FastAPI:
    """Build the app. Tests pass `upstream_transport` and `model_client` to swap the network for fakes."""
    settings = settings or Settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: open the database and the shared upstream client, start checkpointing the
        # database in the background (requests never do it, docs/decisions/0008), and start the
        # background worker if it has a key (§9).
        # mode=0o700: the database holds conversation bodies, so only the owner may read it (§11).
        settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        store = Store(settings.db_path)
        await store.create_tables()
        app.state.store = store
        app.state.client = create_upstream_client(settings, upstream_transport)
        app.state.worker = create_worker(settings, store, model_client)
        try:
            async with checkpointing(store, settings.wal_checkpoint_seconds), running(app.state.worker):
                try:
                    yield
                finally:
                    # Shutdown: finish recording in-flight exchanges before closing the database.
                    await wait_for_pending_writes(app)
                    await app.state.client.aclose()
        finally:
            await store.close()  # the last connection to close checkpoints what's left

    # docs/redoc/openapi are switched off: those paths belong to the upstream API, and the proxy
    # must not answer anything itself.
    app = FastAPI(title="Liber", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.pending_writes = set()
    # The latest request of each recent conversation, so the next one only hashes what's new (ADR 0007).
    app.state.recent_requests = RecentRequests()
    app.state.blobs = BlobStore(settings.blobs_dir)  # offloaded originals (§4)
    app.state.plan_policy = PlanPolicy(  # §6 break points
        idle_apply_seconds=settings.idle_apply_seconds,
        context_threshold=settings.context_threshold,
        threshold_auto_apply=settings.threshold_auto_apply,
    )
    # Order matters: the specific route first, then the catch-all for everything else.
    app.add_api_route("/v1/messages", proxy_messages, methods=["POST"])
    app.add_api_route("/{path:path}", proxy_other, methods=ALL_METHODS)
    return app


async def proxy_messages(request: Request) -> Response:
    """POST /v1/messages: decide what to send, forward it, stream the reply back, record it all."""
    app = request.app
    settings: Settings = app.state.settings
    started = time.perf_counter()
    body = await request.body()

    # §3 steps 2-5. prepare() never raises; on any problem it hands back the client's own bytes.
    prepared = await prepare(app.state.store, body, app.state.recent_requests, app.state.plan_policy)
    record = start_record(request, body, prepared, settings.record_bodies)
    if prepared.offloaded:
        spawn_keep_originals(app, prepared)
    if app.state.worker is not None and prepared.session_id and prepared.parsed is not None:
        app.state.worker.submit(prepared.session_id, prepared.parsed)  # instant; never raises (§9)

    try:
        upstream = await open_upstream(app.state.client, request, prepared.body, prepared.betas)
    except Exception as exc:
        return forwarding_failed(app, record, exc, started)

    try:
        record.status_code = upstream.status_code
        record.upstream_request_id = upstream.headers.get("request-id")
        record.latency_ms = elapsed_ms(started)
        capture = ReplyCapture(upstream.headers.get("content-type", ""), upstream.headers.get("content-encoding"))
        finish = recording_finisher(app, record, capture, started)
        return streaming_response(upstream, relay(upstream, on_chunk=capture.feed, on_end=finish))
    except Exception as exc:
        # Nothing has been sent to the client yet, so it can still get a clean error.
        await upstream.aclose()
        return forwarding_failed(app, record, exc, started)


def create_worker(settings: Settings, store: Store, model_client: ModelClient | None) -> Worker | None:
    """The §9 worker, or None (off) without LIBER_WORKER_API_KEY and without a test's fake client."""
    if model_client is None:
        if settings.worker_api_key is None:
            log.info("worker_off", reason="LIBER_WORKER_API_KEY is not set")
            return None
        model_client = AnthropicModelClient(settings.worker_api_key.get_secret_value(), settings.upstream_base_url)
    return Worker(store, model_client, settings.summary_model, settings.carry_forward_reasoning)


async def proxy_other(request: Request) -> Response:
    """Every other endpoint (token counting, model listing, ...) is passed through unchanged."""
    body = await request.body()
    try:
        upstream = await open_upstream(request.app.state.client, request, body)
    except Exception as exc:
        log_forwarding_failure(request.url.path, exc)
        return gateway_error(exc)
    return streaming_response(upstream, relay(upstream))


def forwarding_failed(app: FastAPI, record: RequestRecord, exc: Exception, started: float) -> Response:
    """The request could not be forwarded: answer with an API-style error and record why."""
    log_forwarding_failure(record.path, exc)
    response = gateway_error(exc)
    record.status_code = response.status_code
    record.error = f"could not forward: {describe(exc)}"
    record.duration_ms = elapsed_ms(started)
    spawn_write(app, record)
    return response


def log_forwarding_failure(path: str, exc: Exception) -> None:
    if isinstance(exc, httpx.HTTPError):
        log.warning("upstream_unreachable", path=path, error=describe(exc))
    else:
        log.exception("forwarding_failed", path=path)  # a Liber bug: keep the traceback


# --- Recording helpers ------------------------------------------------------------------------


def start_record(request: Request, body: bytes, prepared: Prepared, keep_body: bool) -> RequestRecord:
    """Start a `requests` row with what is known before forwarding. Never raises."""
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    try:
        model, stream = request_summary(prepared.request)
        return RequestRecord(
            created_at=datetime.now(UTC),
            method=request.method,
            path=path,
            model=model,
            stream=stream,
            request_headers=recordable_headers(request.headers.items()),
            request_body=body.decode("utf-8", errors="replace") if keep_body else None,
            # What the pipeline decided (§3 steps 2-5).
            session_id=prepared.session_id,
            branch_id=prepared.branch_id,
            known_messages=prepared.known_messages,
            new_messages=prepared.new_messages,
            liber_outcome=prepared.outcome,
            liber_error=prepared.error,
            violations=json.dumps([str(v) for v in prepared.violations]) if prepared.violations else None,
            overhead_ms=prepared.overhead_ms,
            plan_version=prepared.plan_version,
            edit_skipped=prepared.edit_skipped,
            context_share=prepared.context_share,
        )
    except Exception:
        log.exception("recording_failed", path=path)
        return RequestRecord(created_at=datetime.now(UTC), method=request.method, path=path)


def request_summary(decoded: Any) -> tuple[str | None, bool | None]:
    """The model name and stream flag of a decoded request body; Nones if it isn't an object."""
    if not isinstance(decoded, dict):
        return None, None
    model = decoded.get("model")
    stream = decoded.get("stream", False)  # the API's default is non-streaming
    return (
        model if isinstance(model, str) else None,
        stream if isinstance(stream, bool) else None,
    )


def recording_finisher(
    app: FastAPI, record: RequestRecord, capture: ReplyCapture, started: float
) -> Callable[[str | None], None]:
    """The `on_end` callback for relay(): completes the record once the body has ended."""
    keep_body: bool = app.state.settings.record_bodies

    def finish(relay_error: str | None) -> None:
        # Recording is best effort: a failure here is logged and must never affect the client.
        try:
            record.duration_ms = elapsed_ms(started)
            fill_from_reply(record, capture.result(), keep_body)
            problems = [p for p in (relay_error, record.error) if p]
            record.error = "; ".join(problems) or None
            spawn_write(app, record)
        except Exception:
            log.exception("recording_failed", path=record.path)

    return finish


def fill_from_reply(record: RequestRecord, reply: CapturedReply, keep_body: bool) -> None:
    record.stop_reason = reply.stop_reason
    record.input_tokens = reply.usage.input_tokens
    record.output_tokens = reply.usage.output_tokens
    record.cache_read_input_tokens = reply.usage.cache_read_input_tokens
    record.cache_creation_input_tokens = reply.usage.cache_creation_input_tokens
    record.response_body = reply.body_text if keep_body else None
    record.error = reply.problem
    if reply.input_transformations is not None:  # ADR 0004 A4: what the API dropped, if anything
        record.input_transformations = json.dumps(reply.input_transformations)


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


def spawn_keep_originals(app: FastAPI, prepared: Prepared) -> None:
    """Store the originals of the items this request's plan offloads, in a background task (§4)."""
    task = asyncio.create_task(_keep_originals(app, prepared))
    app.state.pending_writes.add(task)
    task.add_done_callback(app.state.pending_writes.discard)


async def _keep_originals(app: FastAPI, prepared: Prepared) -> None:
    try:
        await keep_originals(app.state.store, app.state.blobs, prepared.offloaded)
    except Exception:
        log.exception("keeping_offloaded_originals_failed", session_id=prepared.session_id)


async def wait_for_pending_writes(app: FastAPI) -> None:
    """Wait for every background database write to finish (used at shutdown and by tests)."""
    while app.state.pending_writes:
        await asyncio.gather(*app.state.pending_writes)
