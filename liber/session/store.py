"""Database access: the SQLite store behind Liber (ARCHITECTURE.md §8).

Session 1 needs one table, `requests`: one row per POST /v1/messages with what was asked,
what came back, the token usage (D9) and the latency. Later sessions add `sessions`,
`chain_nodes`, `items`, `plans`, and so on, in this module.

Privacy (hard rule 2, §11): credential headers are never stored. Only the request headers in
`RECORDED_REQUEST_HEADERS` are kept (an allowlist, so a new or unexpected header is never
stored by accident). Bodies are stored only when `Settings.record_bodies` is on.

Tables are created with `create_all` at startup; Alembic migrations come once the schema
settles (§8).
"""

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from sqlalchemy import DateTime, Text, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# The only request headers worth keeping: they change how the API behaves. Never credentials.
RECORDED_REQUEST_HEADERS = frozenset({"anthropic-version", "anthropic-beta", "content-type", "user-agent"})


def recordable_headers(headers: Iterable[tuple[str, str]]) -> str:
    """JSON of the allowlisted headers (repeated ones joined with ", ")."""
    kept: dict[str, str] = {}
    for name, value in headers:
        key = name.lower()
        if key in RECORDED_REQUEST_HEADERS:
            kept[key] = f"{kept[key]}, {value}" if key in kept else value
    return json.dumps(kept)


class Base(DeclarativeBase):
    pass


class RequestRecord(Base):
    """One POST /v1/messages that went through the proxy."""

    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    method: Mapped[str]
    path: Mapped[str] = mapped_column(Text)

    # What the client asked for.
    model: Mapped[str | None]
    stream: Mapped[bool | None]
    request_headers: Mapped[str | None] = mapped_column(Text)  # JSON, allowlisted headers only
    request_body: Mapped[str | None] = mapped_column(Text)  # None when record_bodies is off

    # What came back. status_code is what the client received (502/504 if upstream was down).
    status_code: Mapped[int | None]
    upstream_request_id: Mapped[str | None]  # the API's `request-id` header, for support tickets
    response_body: Mapped[str | None] = mapped_column(Text)  # reassembled Message, or raw body
    stop_reason: Mapped[str | None]

    # Token usage from the API's `usage` field (D9).
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cache_read_input_tokens: Mapped[int | None]
    cache_creation_input_tokens: Mapped[int | None]

    # latency_ms: until upstream status + headers arrived. duration_ms: until the body ended.
    latency_ms: Mapped[float | None]
    duration_ms: Mapped[float | None]

    # Anything that went wrong: upstream error type, parse problem, client disconnect.
    error: Mapped[str | None] = mapped_column(Text)


class Store:
    """Async access to the SQLite database at `db_path`."""

    def __init__(self, db_path: Path) -> None:
        # hide_parameters: if an insert fails, SQLAlchemy's error message would otherwise quote
        # the row's values (conversation text) into the logs.
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", hide_parameters=True)
        # expire_on_commit=False: records stay readable after their session closes.
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_tables(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def add_request(self, record: RequestRecord) -> None:
        async with self._sessions() as session, session.begin():
            session.add(record)

    async def list_requests(self) -> list[RequestRecord]:
        async with self._sessions() as session:
            result = await session.scalars(select(RequestRecord).order_by(RequestRecord.id))
            return list(result)

    async def close(self) -> None:
        await self.engine.dispose()
