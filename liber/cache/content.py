"""The content-addressed cache: what Liber knows about a piece of content, by its hash (ARCHITECTURE.md §7).

    content_hash -> {blob path, tokens, summary, topic}

Hashes never change, so an entry stays valid in every session that contains the same content.
Session 4a fills in blob paths (offloaded items). Exact token counts, summaries and topics are
the worker's (Session 5); an in-memory LRU layer in front of the table can come with it, once
something reads entries on the request path.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Connection, Table, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from liber.cache.blobs import BlobStore
from liber.model.items import Item
from liber.session import chain
from liber.session.store import ContentCacheRecord, Store


@dataclass(frozen=True)
class ContentEntry:
    content_hash: str
    blob_path: str | None
    tokens: int | None
    summary: str | None
    topic: str | None


def lookup(db: Connection, content_hash: str) -> ContentEntry | None:
    row = db.execute(
        select(ContentCacheRecord.__table__).where(ContentCacheRecord.content_hash == content_hash)
    ).first()
    if row is None:
        return None
    return ContentEntry(row.content_hash, row.blob_path, row.tokens, row.summary, row.topic)


def record_blob(db: Connection, content_hash: str, blob_path: str, now: datetime) -> None:
    values = {"blob_path": blob_path, "updated_at": now}
    db.execute(
        sqlite_insert(cast(Table, ContentCacheRecord.__table__))
        .values(content_hash=content_hash, **values)
        .on_conflict_do_update(index_elements=["content_hash"], set_=values)
    )


async def keep_originals(store: Store, blobs: BlobStore, items: list[Item]) -> int:
    """Store offloaded items' originals and note where they are. Returns how many were new.

    For the app's background tasks: file writes happen in a worker thread, never on the
    request path. A blob that already exists is left alone.
    """

    def write_new() -> list[tuple[str, str]]:
        written = []
        for item in items:
            if blobs.get(item.content_hash) is None:
                data = chain.canonical_block(item.block).encode("utf-8")  # what content_hash covers
                written.append((item.content_hash, str(blobs.put(item.content_hash, data))))
        return written

    written = await asyncio.to_thread(write_new)
    if written:
        now = datetime.now(UTC)
        await store.run(lambda db: record_blobs(db.connection(), written, now))
    return len(written)


def record_blobs(db: Connection, written: list[tuple[str, str]], now: datetime) -> None:
    for content_hash, path in written:
        record_blob(db, content_hash, path, now)
