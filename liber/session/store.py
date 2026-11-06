"""Database access: the SQLite store behind Liber (ARCHITECTURE.md §8).

Tables so far:
- `requests`: one row per POST /v1/messages: what was asked, what came back, token usage (D9),
  latency, and what Liber did with it (session, outcome, validation result).
- `sessions`, `branches`, `chain_nodes`: the conversation tree built by session/chain.py (D7).
  A chain node is one message (identified by its chain hash); a branch is a path through the
  tree; a session is the family of branches that grew from the same first message.
- `items`: one row per content block (§4). Items belong to the message that holds them, so
  branches that share a prefix share its items.
- `plan_versions`, `plans`: each session's edit plan as immutable versions, and which version
  is applied or pending (plan/versions.py).
- `content_cache`: where offloaded originals are kept, by content hash (§7, cache/content.py).
- `exchange_tokens`, `exchange_summaries`: the background worker's exact token counts and
  summaries, by exchange content (§9, cache/exchanges.py).

Privacy (hard rule 2, §11): credential headers are never stored. Only the request headers in
`RECORDED_REQUEST_HEADERS` are kept (an allowlist, so a new or unexpected header is never
stored by accident). Bodies are stored only when `Settings.record_bodies` is on; the
conversation tables hold hashes, positions and token counts, never content. Content the user
chose to edit is the exception: an offloaded item's original goes to the blob store (it must
be restorable), a plan holds its summaries' text, and the worker's summaries are kept per
exchange (only when the worker is on, LIBER_WORKER_API_KEY).

Tables are created with `create_all` at startup, and columns added since an older version are
added in place (`add_missing_columns`). Alembic migrations come once the schema settles (§8).

Checkpoints (docs/decisions/0008): commits never copy the write-ahead log into the database
file themselves (`configure_sqlite`); a background task the app runs (`checkpointing`) does it
instead, so that work never lands on the request path.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar, cast

import structlog
from sqlalchemy import Connection, DateTime, Table, Text, bindparam, create_engine, event, inspect, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

T = TypeVar("T")

# The request path's writes (identification, applying a plan) wait at most this long for another
# writer to finish before Liber steps aside (§3 budget, docs/decisions/0003).
SQLITE_BUSY_TIMEOUT_MS = 100


class StoreBusy(Exception):
    """The database is busy; this request goes through without Liber rather than wait (§3 budget)."""


log = structlog.get_logger(__name__)

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
    plan_version: Mapped[int | None]  # the session's plan version rendered for this request (None: no plan)
    edit_skipped: Mapped[str | None] = mapped_column(Text)  # why a plan wasn't used or its edit not sent
    # Estimated share of the model's context window the request fills (§6 threshold break point).
    context_share: Mapped[float | None]
    # What the API changed in the request before the model saw it, e.g. dropped thinking blocks
    # (ADR 0004 A4): JSON, as the response reported it. None when the response didn't say.
    input_transformations: Mapped[str | None] = mapped_column(Text)


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


class PlanVersionRecord(Base):
    """One immutable version of a session's edit plan (§4). Version 0, the empty plan, isn't stored."""

    __tablename__ = "plan_versions"

    session_id: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)  # 1, 2, 3... per session
    parent_version: Mapped[int]  # the version it was built on; undo goes back to it (0: none)
    plan: Mapped[str] = mapped_column(Text)  # the Plan as JSON
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PlanStateRecord(Base):
    """Which plan versions a session uses (§4): the applied one, and a pending one waiting for a break point."""

    __tablename__ = "plans"

    session_id: Mapped[str] = mapped_column(primary_key=True)
    applied_version: Mapped[int]  # used on every request; 0 = the empty plan
    pending_version: Mapped[int | None]
    apply_requested: Mapped[bool]  # the user asked for pending to be applied (§6 break point 1)
    # Messages in the request where the applied version took effect. Thinking blocks before that
    # point were produced against other history, so they are stripped (docs/decisions/0012).
    activated_at: Mapped[int | None]
    # Chain hash of that request's last message: a later request continues the same history only
    # if its own chain hash at that position is this one (another branch doesn't).
    activated_hash: Mapped[str | None]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ContentCacheRecord(Base):
    """Where an offloaded original is kept, by its content hash (§7, cache/content.py).

    (Databases from before Session 5 also have tokens/summary/topic columns here, never filled:
    the worker's results depend on the model, so they live in the two tables below.)
    """

    __tablename__ = "content_cache"

    content_hash: Mapped[str] = mapped_column(primary_key=True)
    blob_path: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExchangeTokensRecord(Base):
    """An exchange's exact token count from the counting endpoint (D9, cache/exchanges.py).

    Counts depend on the model's tokenizer, so the key includes the model.
    """

    __tablename__ = "exchange_tokens"

    exchange_key: Mapped[str] = mapped_column(primary_key=True)
    model: Mapped[str] = mapped_column(primary_key=True)
    tokens: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExchangeSummaryRecord(Base):
    """The worker's summary and topic of an exchange (D10, cache/exchanges.py).

    `summarizer` names the model and prompt version that wrote it: a different summarizer writes
    its own row. This is conversation content derived from the client's history, kept so a plan
    can be made from another process (the CLI).
    """

    __tablename__ = "exchange_summaries"

    exchange_key: Mapped[str] = mapped_column(primary_key=True)
    summarizer: Mapped[str] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)  # carry_forward_reasoning only (ADR 0004 A5)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
            if column.name in existing:
                continue
            if not column.nullable:
                # Existing rows would have no value for it: that needs a real migration.
                raise RuntimeError(
                    f"{table.name}.{column.name} is missing from {connection.engine.url.database} and is "
                    "required; this database needs a migration (or delete it to start fresh)"
                )
            column_type = column.type.compile(dialect=connection.dialect)
            connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column_type}"))
        for index in table.indexes:  # create_all skips indexes of tables that already exist
            index.create(connection, checkfirst=True)


def configure_sqlite(dbapi_connection: Any, connection_record: Any) -> None:
    """Settings applied to every new SQLite connection.

    WAL journaling with synchronous=NORMAL is the usual setup for an application database:
    commits append to a log instead of syncing the whole file to disk, which keeps the request
    path fast. A crash can't corrupt the database; a power loss can lose the last few writes.

    wal_autocheckpoint=0: by default the commit that takes the log past 1000 pages also copies
    the log back into the database file (a checkpoint, ~10 ms), and that commit can be a
    request's. Every connection here can serve a request, so none of them checkpoint;
    checkpoint_regularly does it in the background (docs/decisions/0008).
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA wal_autocheckpoint=0")
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

    async def run(self, work: Callable[[Session], T], busy_timeout_ms: int = 5000, immediate: bool = False) -> T:
        """Run `work(db)` in one transaction on a worker thread; commit if it returns normally.

        busy_timeout_ms: how long to wait if another connection holds SQLite's write lock.
        immediate: take the write lock at the start (BEGIN IMMEDIATE) instead of at the first
        write, so everything `work` reads stays true until it commits, even across processes.
        """
        return await asyncio.to_thread(self._run_now, work, busy_timeout_ms, immediate)

    def _run_now(self, work: Callable[[Session], T], busy_timeout_ms: int = 5000, immediate: bool = False) -> T:
        with self._sessions() as db, db.begin():
            connection = db.connection()
            # Set on every transaction: pooled connections are shared by callers with different needs.
            connection.exec_driver_sql(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            if immediate:
                # Python's sqlite3 would otherwise start the transaction only at the first write.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
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

    async def checkpoint(self) -> tuple[int, int, int]:
        """Copy the write-ahead log into the database file, in a worker thread.

        PASSIVE: copies what it can without waiting for other connections, which carry on
        reading and writing meanwhile, so a request running at the same time isn't held up.
        Returns SQLite's (busy, pages in the log, pages copied). Once every page is copied, the
        next commit starts the log over from the beginning, which is what keeps it from growing.
        """

        def run_checkpoint() -> tuple[int, int, int]:
            # No transaction is open here (Python's sqlite3 only opens one before a write), which
            # SQLite requires: a connection inside a transaction can't checkpoint.
            with self.engine.connect() as connection:
                busy, log_pages, copied = connection.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)").one()
            return busy, log_pages, copied

        return await asyncio.to_thread(run_checkpoint)

    async def close(self) -> None:
        # The last connection to close also checkpoints and deletes the log (SQLite does this).
        await asyncio.to_thread(self.engine.dispose)


@asynccontextmanager
async def checkpointing(store: Store, every_seconds: float) -> AsyncIterator[None]:
    """Checkpoint `store` in the background while the `async with` block runs.

    The app wraps its whole lifetime in this (liber/app.py). On leaving the block, a checkpoint
    already in progress is allowed to finish, so the store is never closed under it.
    """
    stop = asyncio.Event()
    task = asyncio.create_task(checkpoint_regularly(store, every_seconds, stop))
    try:
        yield
    finally:
        stop.set()
        await task


async def checkpoint_regularly(store: Store, every_seconds: float, stop: asyncio.Event) -> None:
    """Checkpoint the store every `every_seconds` until `stop` is set.

    The log then holds about `every_seconds` worth of writes. A failed checkpoint is logged and
    tried again next time: nothing is lost meanwhile, the log just grows a little longer.
    """
    while True:
        try:
            # Wait out the interval, unless told to stop first.
            await asyncio.wait_for(stop.wait(), timeout=every_seconds)
            return
        except TimeoutError:
            pass
        try:
            await store.checkpoint()
        except Exception:
            log.exception("wal_checkpoint_failed")


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
    last_seen_at: datetime  # the branch's previous request (before this one)


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
            BranchRecord.last_seen_at,
        )
        .join(BranchRecord, BranchRecord.id == ChainNode.branch_id)
        # One expanding parameter for the whole list: the compiled query is cached and reused.
        .where(ChainNode.hash.in_(bindparam("hashes", expanding=True)))
        .order_by(ChainNode.depth.desc())
        .limit(1)
    )
    row = db.execute(query, {"hashes": hashes}).first()
    return KnownNode(**row._mapping) if row is not None else None  # by column name, not position


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
