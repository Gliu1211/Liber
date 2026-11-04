"""SQLite's write-ahead log is checkpointed in the background, never by a request (ADR 0008).

In WAL mode every commit appends to `liber.db-wal`; a *checkpoint* copies those pages back into
`liber.db` so the log can start over. By default SQLite checkpoints inside whichever commit
takes the log past 1000 pages, and that commit was sometimes identification's, on the request
path (~10 ms). Liber turns that off and checkpoints from a background task instead.
"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from liber.app import create_app
from liber.config import Settings
from liber.session.store import RequestRecord, Store, checkpointing

SQLITE_DEFAULT_AUTOCHECKPOINT_PAGES = 1000


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    store = Store(tmp_path / "liber.db")
    await store.create_tables()
    yield store
    await store.close()


def record(body_size: int = 0) -> RequestRecord:
    return RequestRecord(created_at=datetime.now(UTC), method="POST", path="/v1/messages", request_body="x" * body_size)


async def test_commits_never_checkpoint(store: Store) -> None:
    # One commit of ~5 MB (well past SQLite's 1000-page threshold), then a small one. With
    # automatic checkpoints the first commit would have checkpointed and the second would have
    # started the log over; here both are still in the log, waiting for the background task.
    await store.add_request(record(body_size=5_000_000))
    await store.add_request(record())

    autocheckpoint = await store.run(lambda db: db.connection().exec_driver_sql("PRAGMA wal_autocheckpoint").scalar())
    busy, wal_pages, copied = await store.checkpoint()

    assert autocheckpoint == 0
    assert busy == 0
    assert wal_pages > SQLITE_DEFAULT_AUTOCHECKPOINT_PAGES
    assert copied == wal_pages  # the checkpoint copied all of it


async def test_after_a_checkpoint_the_log_starts_over(store: Store) -> None:
    await store.add_request(record(body_size=500_000))
    _, before, _ = await store.checkpoint()

    await store.add_request(record())
    _, after, _ = await store.checkpoint()

    assert before > 100
    assert after < 10  # the next write went to the start of the log: it doesn't keep growing


async def test_checkpoints_keep_running_after_a_failure(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    third_call = asyncio.Event()

    async def flaky_checkpoint() -> tuple[int, int, int]:
        nonlocal calls
        calls += 1
        if calls == 3:
            third_call.set()
        if calls == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return 0, 0, 0

    monkeypatch.setattr(store, "checkpoint", flaky_checkpoint)
    async with checkpointing(store, every_seconds=0.001), asyncio.timeout(2):
        await third_call.wait()  # times out (and fails the test) if the loop died after the first error

    assert calls >= 3


async def test_leaving_the_block_waits_for_a_checkpoint_in_progress(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Otherwise the app could close the store under a running checkpoint at shutdown.
    started, finished = asyncio.Event(), asyncio.Event()

    async def slow_checkpoint() -> tuple[int, int, int]:
        started.set()
        await asyncio.sleep(0.05)
        finished.set()
        return 0, 0, 0

    monkeypatch.setattr(store, "checkpoint", slow_checkpoint)
    async with checkpointing(store, every_seconds=0.001):
        await started.wait()

    assert finished.is_set()


def test_the_checkpoint_interval_must_be_positive() -> None:
    # 0 would make the background task checkpoint in a tight loop.
    with pytest.raises(ValidationError):
        Settings(wal_checkpoint_seconds=0)


async def test_the_app_checkpoints_in_the_background_until_shutdown(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    real_checkpoint = Store.checkpoint

    async def counting_checkpoint(self: Store) -> tuple[int, int, int]:
        nonlocal calls
        calls += 1
        return await real_checkpoint(self)

    monkeypatch.setattr(Store, "checkpoint", counting_checkpoint)
    settings.wal_checkpoint_seconds = 0.01
    app = create_app(settings, upstream_transport=httpx.MockTransport(lambda request: httpx.Response(200)))

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.1)
    after_shutdown = calls
    await asyncio.sleep(0.05)

    assert after_shutdown >= 2
    assert calls == after_shutdown  # stopped with the app


async def test_closing_the_store_leaves_no_log_behind(tmp_path: Path) -> None:
    # The last connection to close checkpoints and removes the log, as SQLite always does.
    store = Store(tmp_path / "liber.db")
    await store.create_tables()
    await store.add_request(record(body_size=100_000))

    await store.close()

    assert not (tmp_path / "liber.db-wal").exists()
