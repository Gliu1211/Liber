"""Database access: the SQLite store behind Liber (ARCHITECTURE.md §8).

Tables so far:
- `requests`: one row per POST /v1/messages: what was asked, what came back, token usage (D9),
  latency, and what Liber did with it (session, outcome, validation result).
- `sessions`, `branches`, `chain_nodes`: the conversation tree built by session/chain.py (D7).
  A chain node is one message (identified by its chain hash); a branch is a path through the
  tree; a session is the family of branches that grew from the same first message.
- `items`: one row per content block (§4). Items belong to the message that holds them, so
  branches that share a prefix share its items.

Privacy (hard rule 2, §11): credential headers are never stored. Only the request headers in
`RECORDED_REQUEST_HEADERS` are kept (an allowlist, so a new or unexpected header is never
stored by accident). Bodies are stored only when `Settings.record_bodies` is on; the
conversation tables hold hashes, positions and token counts, never content.

Tables are created with `create_all` at startup, and columns added since an older version are
added in place (`add_missing_columns`). Alembic migrations come once the schema settles (§8).
"""

import asyncio
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from sqlalchemy import Connection, DateTime, Table, Text, bindparam, create_engine, event, inspect, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

T = TypeVar("T")

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

    # What Liber did before forwarding (§3 steps 2-5, liber/pipeline.py).
    session_id: Mapped[str | None]
    branch_id: Mapped[str | None]
    known_messages: Mapped[int | None]  # messages Liber had already seen in this conversation
    new_messages: Mapped[int | None]
    liber_outcome: Mapped[str | None]  # "passthrough", "rendered" or "fallback"
    liber_error: Mapped[str | None] = mapped_column(Text)  # why it fell back, if it did
    violations: Mapped[str | None] = mapped_column(Text)  # JSON list of validator messages
    overhead_ms: Mapped[float | None]  # time spent in §3 steps 2-5


class SessionRecord(Base):
    """A conversation: every branch that grew from the same first message (D7)."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(primary_key=True)
    root_hash: Mapped[str]  # h_0 = H(system || tools)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # When it was last used = the latest last_seen_at of its branches (kept there only, so a
    # request updates one row instead of two).


class BranchRecord(Base):
    """A path through a session's message tree. A request belongs to exactly one branch."""

    __tablename__ = "branches"

    id: Mapped[str] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(index=True)
    parent_branch_id: Mapped[str | None]  # None for the session's first branch
    fork_depth: Mapped[int]  # messages shared with the parent branch
    head_hash: Mapped[str]  # chain hash of the branch's newest message
    head_depth: Mapped[int]  # number of messages on the branch
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChainNode(Base):
    """One message in the tree, identified by its chain hash h_i (D7)."""

    __tablename__ = "chain_nodes"

    hash: Mapped[str] = mapped_column(primary_key=True)
    parent_hash: Mapped[str]  # h_{i-1}; for the first message, the root hash h_0
    session_id: Mapped[str] = mapped_column(index=True)
    branch_id: Mapped[str]  # the branch that first contained this message
    depth: Mapped[int]  # 1 for the first message
    role: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ItemRecord(Base):
    """One content block (§4). Content itself is not stored here, only what describes it."""

    __tablename__ = "items"

    id: Mapped[str] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(index=True)
    node_hash: Mapped[str] = mapped_column(index=True)  # the message holding this block
    msg_index: Mapped[int]
    block_index: Mapped[int]
    role: Mapped[str]
    kind: Mapped[str]
    content_hash: Mapped[str] = mapped_column(index=True)
    tokens: Mapped[int]
    tokens_exact: Mapped[bool]
    topic: Mapped[str | None]
    tool_use_id: Mapped[str | None]
    depends_on: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def add_missing_columns(connection: Connection) -> None:
    """Add columns that exist in the models but not yet in the database file.

    `create_all` creates missing tables but never changes existing ones, so a database from an
    older Liber would lack new columns and every insert would fail. New columns are always
    nullable, so adding them in place is safe; old rows get NULL.
    """
    inspector = inspect(connection)
    for table in Base.metadata.sorted_tables:
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing:
                column_type = column.type.compile(dialect=connection.dialect)
                connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column_type}"))


def configure_sqlite(dbapi_connection: Any, connection_record: Any) -> None:
    """Settings applied to every new SQLite connection.

    WAL journaling with synchronous=NORMAL is the usual setup for an application database:
    commits append to a log instead of syncing the whole file to disk, which keeps the request
    path fast. A crash can't corrupt the database; a power loss can lose the last few writes.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


class Store:
    """Async access to the SQLite database at `db_path`.

    SQLite is a local file and its queries take microseconds, but they block. Each Store method
    therefore does its whole job (one transaction) in a worker thread via `asyncio.to_thread`, so
    the event loop keeps serving other requests. One thread hop per operation instead of one per
    SQL statement is what keeps the request path within its budget (docs/decisions/0003).
    """

    def __init__(self, db_path: Path) -> None:
        # hide_parameters: if an insert fails, SQLAlchemy's error message would otherwise quote
        # the row's values (conversation text) into the logs.
        self.engine = create_engine(f"sqlite:///{db_path}", hide_parameters=True)
        event.listen(self.engine, "connect", configure_sqlite)
        # expire_on_commit=False: records stay readable after their session closes.
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        # One conversation update at a time (see session/chain.py:identify).
        self.lock = asyncio.Lock()

    async def run(self, work: Callable[[Session], T]) -> T:
        """Run `work(db)` in one transaction on a worker thread; commit if it returns normally."""
        return await asyncio.to_thread(self._run_now, work)

    def _run_now(self, work: Callable[[Session], T]) -> T:
        with self._sessions() as db, db.begin():
            return work(db)

    async def create_tables(self) -> None:
        def create(db: Session) -> None:
            connection = db.connection()
            Base.metadata.create_all(connection)
            add_missing_columns(connection)

        await self.run(create)

    async def add_request(self, record: RequestRecord) -> None:
        await self.run(lambda db: db.add(record))

    async def list_requests(self) -> list[RequestRecord]:
        return await self.run(lambda db: list(db.scalars(select(RequestRecord).order_by(RequestRecord.id))))

    async def list_sessions(self) -> list[SessionRecord]:
        return await self.run(lambda db: list(db.scalars(select(SessionRecord).order_by(SessionRecord.created_at))))

    async def list_items(self, session_id: str | None) -> list[ItemRecord]:
        query = (
            select(ItemRecord)
            .where(ItemRecord.session_id == session_id)
            .order_by(ItemRecord.msg_index, ItemRecord.block_index, ItemRecord.created_at)
        )
        return await self.run(lambda db: list(db.scalars(query)))

    async def close(self) -> None:
        await asyncio.to_thread(self.engine.dispose)


# --- Queries used by session/chain.py, inside its transaction -------------------------------
#
# Plain SQL on the transaction's Connection rather than through the ORM Session: these run on
# every request, and the ORM's bookkeeping cost several times more than SQLite's own work.


@dataclass(frozen=True)
class KnownNode:
    """The deepest stored message of a request, and the branch it was first seen on."""

    hash: str
    depth: int
    session_id: str
    branch_id: str
    head_hash: str  # the branch's newest message
    parent_branch_id: str | None
    fork_depth: int


def deepest_known_node(db: Connection, hashes: list[str]) -> KnownNode | None:
    """Of the given chain hashes, the stored node with the greatest depth, plus its branch.

    Because each hash covers everything before it, if h_k is stored then so are h_1..h_(k-1);
    the deepest stored hash is therefore the end of the longest matching prefix. Fetched together
    with its branch: everything identify() needs, in one query.
    """
    query = (
        select(
            ChainNode.hash,
            ChainNode.depth,
            ChainNode.session_id,
            ChainNode.branch_id,
            BranchRecord.head_hash,
            BranchRecord.parent_branch_id,
            BranchRecord.fork_depth,
        )
        .join(BranchRecord, BranchRecord.id == ChainNode.branch_id)
        # One expanding parameter for the whole list: the compiled query is cached and reused.
        .where(ChainNode.hash.in_(bindparam("hashes", expanding=True)))
        .order_by(ChainNode.depth.desc())
        .limit(1)
    )
    row = db.execute(query, {"hashes": hashes}).first()
    return KnownNode(*row) if row is not None else None


def touch_branch(db: Connection, branch_id: str, now: datetime, new_head: tuple[str, int] | None) -> None:
    """Mark a branch as just used, and move its head forward if the request added messages."""
    values: dict[str, Any] = {"last_seen_at": now}
    if new_head is not None:
        values["head_hash"], values["head_depth"] = new_head
    db.execute(update(BranchRecord).where(BranchRecord.id == branch_id).values(**values))


def insert_new(db: Connection, model: type[Base], rows: list[dict[str, Any]]) -> None:
    """Insert rows in one statement, skipping any whose primary key already exists.

    Nodes and items are content-addressed, so a row that already exists is the same row. The
    plain table (not the ORM class) is used so the rows go straight to SQLite in one batch.
    """
    if rows:
        table = cast(Table, model.__table__)
        db.execute(sqlite_insert(table).on_conflict_do_nothing(), rows)
