"""The background worker: one asyncio task in the proxy's process (ARCHITECTURE.md §9).

The request path hands it each request it has identified (`submit`: no await, no I/O, never
raises), then goes on. The worker, on its own time:

    for each completed exchange of the request         (cache/exchanges.py)
        exact token count, if not cached for this model  (worker/tokens.py)
        summary + topic, if not cached                   (worker/summarize.py)
        carry-forward notes, if the setting is on and they aren't cached
        each result written to the database as soon as it's made

Hard rule 4: nothing here runs on the request path, and the request path never waits for it.
A failure is logged and stays here: `submit` swallows its own errors, every exchange is handled
in its own try, and the loop survives anything a job does.

Waiting jobs are kept per session, and a newer request replaces its session's older one: each
request carries the whole history, so the newest has everything the older one had. At most
MAX_WAITING sessions wait; past that the oldest is dropped (its next request brings it back).
One task works through them in turn, so two requests holding the same exchange can't both
summarize it: the second finds it in the cache.

Retries (`with_retries`): a call the model can't take now (rate limits, overload, the network)
is tried again after 1, 2, 4 s; if it still fails, the exchange is left for the session's next
request. A call that can't succeed (a refusal, a reply of the wrong shape, a bad request) isn't
tried again until the proxy restarts.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, TypeVar

import structlog

from liber.cache import exchanges
from liber.cache.exchanges import Exchange
from liber.model.anthropic import ParsedRequest
from liber.model.items import ExchangeSummary, Item
from liber.session.store import Store
from liber.worker import summarize
from liber.worker.client import ModelClient, ModelUnavailable, with_retries
from liber.worker.tokens import TokenCounter

log = structlog.get_logger(__name__)

MAX_WAITING = 32

T = TypeVar("T")


@dataclass(frozen=True)
class Job:
    session_id: str
    model: str | None  # the session's model: token counts are made with its tokenizer
    items: tuple[Item, ...]  # the request's items, blocks included; held in memory only


@dataclass(frozen=True)
class Retry:
    attempts: int = 4
    first_delay_seconds: float = 1.0


DEFAULT_RETRY = Retry()  # 1 s, 2 s, 4 s between the four attempts


class Worker:
    def __init__(
        self,
        store: Store,
        client: ModelClient,
        summary_model: str,
        carry_forward_reasoning: bool = False,
        retry: Retry = DEFAULT_RETRY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.store = store
        self.client = client
        self.summary_model = summary_model
        self.summarizer = summarize.summarizer_id(summary_model)
        self.carry_forward_reasoning = carry_forward_reasoning
        self.retry = retry
        self.sleep = sleep
        self.counter = TokenCounter(client)
        self.waiting: dict[str, Job] = {}  # by session, oldest first
        self.wake = asyncio.Event()
        self.gave_up: set[tuple[str, str]] = set()  # (what, exchange key) that can't succeed

    def submit(self, session_id: str, parsed: ParsedRequest) -> None:
        """Queue a request's history for the worker. For the request path: instant, never raises."""
        try:
            model = parsed.original.get("model")
            job = Job(session_id, model if isinstance(model, str) else None, tuple(parsed.items))
            self.waiting.pop(session_id, None)  # re-inserted at the end: the newest waits longest
            self.waiting[session_id] = job
            while len(self.waiting) > MAX_WAITING:
                dropped = next(iter(self.waiting))
                del self.waiting[dropped]
                log.info("worker_job_dropped", session_id=dropped)
            self.wake.set()
        except Exception as exc:
            log.warning("worker_submit_failed", error=type(exc).__name__)

    async def run(self) -> None:
        """Work through waiting jobs until cancelled."""
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.waiting:
                session_id = next(iter(self.waiting))
                job = self.waiting.pop(session_id)
                try:
                    await self.process(job)
                except Exception as exc:  # a bug here must not stop the worker
                    log.warning("worker_job_failed", session_id=session_id, error=type(exc).__name__)

    async def process(self, job: Job) -> None:
        """Count and summarize the job's completed exchanges that aren't in the cache yet."""
        found = exchanges.completed_exchanges(job.items)
        if not found:
            return
        keys = [exchange.key for exchange in found]
        model = job.model

        def read(db: Any) -> tuple[dict[str, int], dict[str, ExchangeSummary]]:
            connection = db.connection()
            tokens = exchanges.cached_tokens(connection, keys, model) if model else {}
            return tokens, exchanges.cached_summaries(connection, keys, self.summarizer)

        known_tokens, known_summaries = await self.store.run(read)
        for exchange in found:
            if model and exchange.key not in known_tokens:
                await self.attempt("count", exchange, partial(self.count, model, exchange))
            summary = known_summaries.get(exchange.key)
            if summary is None:
                summary = await self.attempt("summary", exchange, partial(self.write_summary, exchange))
            # Notes are a separate call, after the summary: the summary is the same with or without them.
            if summary is not None and self.carry_forward_reasoning and summary.notes is None:
                await self.attempt("notes", exchange, partial(self.write_notes, exchange, summary))

    async def attempt(self, what: str, exchange: Exchange[Item], work: Callable[[], Awaitable[T]]) -> T | None:
        """`work()`, or None if it failed (logged; never raised)."""
        if (what, exchange.key) in self.gave_up:
            return None
        try:
            return await work()
        except ModelUnavailable as exc:
            log.warning("worker_model_unavailable", what=what, reason=str(exc), exchange=exchange.key[:12])
        except Exception as exc:
            self.gave_up.add((what, exchange.key))
            log.warning("worker_gave_up", what=what, error=type(exc).__name__, exchange=exchange.key[:12])
        return None

    async def count(self, model: str, exchange: Exchange[Item]) -> None:
        tokens = await self.retrying(partial(self.counter.count, model, exchange))
        now = datetime.now(UTC)
        await self.store.run(lambda db: exchanges.record_tokens(db.connection(), exchange.key, model, tokens, now))

    async def write_summary(self, exchange: Exchange[Item]) -> ExchangeSummary:
        summary = await self.retrying(partial(summarize.summarize, self.client, self.summary_model, exchange))
        await self.save(exchange, summary)
        return summary

    async def write_notes(self, exchange: Exchange[Item], summary: ExchangeSummary) -> None:
        notes = await self.retrying(partial(summarize.carry_forward_notes, self.client, self.summary_model, exchange))
        await self.save(exchange, replace(summary, notes=notes))

    async def save(self, exchange: Exchange[Item], summary: ExchangeSummary) -> None:
        now = datetime.now(UTC)
        await self.store.run(
            lambda db: exchanges.record_summary(db.connection(), exchange.key, self.summarizer, summary, now)
        )

    async def retrying(self, call: Callable[[], Awaitable[T]]) -> T:
        return await with_retries(call, self.retry.attempts, self.retry.first_delay_seconds, self.sleep)


@asynccontextmanager
async def running(worker: Worker | None) -> AsyncIterator[None]:
    """Run `worker` in the background for the duration of the block (the app's lifespan)."""
    if worker is None:
        yield
        return
    task = asyncio.create_task(worker.run(), name="liber-worker")
    try:
        yield
    finally:
        task.cancel()  # an unfinished summary is lost; the session's next request brings it back
        with suppress(asyncio.CancelledError):
            await task
