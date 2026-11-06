"""The content-addressed cache for offloaded originals: content hash -> blob path (ARCHITECTURE.md §7).

Hashes never change, so an entry stays valid in every session that contains the same content.
The worker's exact token counts, summaries and topics are kept per exchange, and per model,
in cache/exchanges.py (docs/decisions/0013).
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import structlog
from sqlalchemy import Connection, Table, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from liber.cache.blobs import BlobStore
from liber.model.items import Item
from liber.session import chain
from liber.session.store import ContentCacheRecord, Store

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ContentEntry:
    content_hash: str
    blob_path: str | None


def lookup(db: Connection, content_hash: str) -> ContentEntry | None:
    row = db.execute(
        select(ContentCacheRecord.__table__).where(ContentCacheRecord.content_hash == content_hash)
    ).first()
    if row is None:
        return None
    return ContentEntry(row.content_hash, row.blob_path)


def record_blob(db: Connection, content_hash: str, blob_path: str, now: datetime) -> None:
    values = {"blob_path": blob_path, "updated_at": now}
    db.execute(
        sqlite_insert(cast(Table, ContentCacheRecord.__table__))
        .values(content_hash=content_hash, **values)
        .on_conflict_do_update(index_elements=["content_hash"], set_=values)
    )


async def keep_originals(store: Store, blobs: BlobStore, items: list[Item]) -> int:
    """Store offloaded items' originals and note where they are. Returns how many were stored.

    For the app's background tasks: file writes happen in a worker thread, never on the
    request path. An intact blob is left alone; each item is handled on its own, so one failure
    doesn't lose the others.
    """

    def write_all() -> list[tuple[str, str]]:
        stored = []
        for item in items:
            try:
                data = chain.canonical_block(item.block).encode("utf-8")  # what content_hash covers
                stored.append((item.content_hash, str(blobs.put(item.content_hash, data))))
            except Exception as exc:
                log.warning("offloaded_original_not_stored", content_hash=item.content_hash, error=type(exc).__name__)
        return stored

    stored = await asyncio.to_thread(write_all)
    if stored:
        now = datetime.now(UTC)
        await store.run(lambda db: record_blobs(db.connection(), stored, now))
    return len(stored)


def record_blobs(db: Connection, stored: list[tuple[str, str]], now: datetime) -> None:
    """Note each blob's path in the cache, unless it's already noted (no write per request)."""
    for content_hash, path in stored:
        entry = lookup(db, content_hash)
        if entry is None or entry.blob_path != path:
            record_blob(db, content_hash, path, now)
