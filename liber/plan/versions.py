"""Versioned edit plans: pending vs applied, immutable versions, undo (ARCHITECTURE.md §4 "Edit plans").

Each session has numbered plan versions. A version is never changed once written: a new edit
makes a new version, built on the previous one (its parent). The `plans` row says which
version is *applied* (used on every request) and which is *pending* (made, not yet in effect):

    create  -> a new version from the newest one plus new operations; it becomes pending
    apply   -> the user asks for pending to take effect (§6 break point 1). The request path
               promotes it at the next request that ends at a clean turn boundary (ADR 0004 A1)
               and that the plan fits (policy.py, pipeline.py)
    undo    -> the applied version's parent becomes pending, apply requested: the next clean
               turn goes back to exactly the previous render

Version 0 is the empty plan and isn't stored. Plain SQL on the transaction's connection, like
session/chain.py; the async functions run each step as one Store transaction.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import structlog
from sqlalchemy import Connection, Table, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError

from liber.model.items import Operation, Plan
from liber.session.store import SQLITE_BUSY_TIMEOUT_MS, PlanStateRecord, PlanVersionRecord, Store, StoreBusy

log = structlog.get_logger(__name__)


class PlanError(Exception):
    """A plan command can't be carried out (no session, nothing pending, nothing to undo)."""


@dataclass(frozen=True)
class PlanVersion:
    number: int  # 0 = the empty plan
    plan: Plan
    parent: int = 0


EMPTY = PlanVersion(0, Plan())


@dataclass(frozen=True)
class PlanState:
    applied: PlanVersion
    pending: PlanVersion | None
    apply_requested: bool


NO_PLAN = PlanState(applied=EMPTY, pending=None, apply_requested=False)


# --- Inside a transaction -----------------------------------------------------------------------


def read_state(db: Connection, session_id: str) -> PlanState:
    """The session's applied and pending versions (NO_PLAN if it never had a plan)."""
    row = db.execute(select(PlanStateRecord.__table__).where(PlanStateRecord.session_id == session_id)).first()
    if row is None:
        return NO_PLAN
    pending = read_version(db, session_id, row.pending_version) if row.pending_version is not None else None
    return PlanState(read_version(db, session_id, row.applied_version), pending, row.apply_requested)


def read_state_or_no_plan(db: Connection, session_id: str) -> PlanState:
    """read_state for the request path: a damaged plan row means no plan, not a failed request.

    It runs inside identification's transaction, so an error here would also stop the
    conversation from being registered. Logged without the row's content.
    """
    try:
        return read_state(db, session_id)
    except Exception as exc:
        log.warning("plan_state_unreadable", session_id=session_id, error=type(exc).__name__)
        return NO_PLAN


def read_version(db: Connection, session_id: str, number: int) -> PlanVersion:
    if number == 0:
        return EMPTY
    row = db.execute(
        select(PlanVersionRecord.__table__).where(
            PlanVersionRecord.session_id == session_id, PlanVersionRecord.version == number
        )
    ).one()
    return PlanVersion(number, Plan.model_validate_json(row.plan), row.parent_version)


def create_version(db: Connection, session_id: str, operations: tuple[Operation, ...], now: datetime) -> PlanVersion:
    """A new version: the newest one's operations (pending if there is one, else applied) plus these."""
    state = read_state(db, session_id)
    base = state.pending or state.applied
    newest = db.execute(
        select(func.max(PlanVersionRecord.version)).where(PlanVersionRecord.session_id == session_id)
    ).scalar()
    version = PlanVersion((newest or 0) + 1, Plan(operations=base.plan.operations + operations), base.number)
    db.execute(
        sqlite_insert(cast(Table, PlanVersionRecord.__table__)).values(
            session_id=session_id,
            version=version.number,
            parent_version=version.parent,
            plan=version.plan.model_dump_json(),
            created_at=now,
        )
    )
    set_pending(db, session_id, state.applied.number, version.number, requested=False, now=now)
    return version


def request_apply(db: Connection, session_id: str, now: datetime) -> PlanVersion:
    state = read_state(db, session_id)
    if state.pending is None:
        raise PlanError("there is no pending plan version to apply")
    set_pending(db, session_id, state.applied.number, state.pending.number, requested=True, now=now)
    return state.pending


def request_undo(db: Connection, session_id: str, now: datetime) -> PlanVersion:
    """Go back to the version before the applied one, at the next clean turn boundary."""
    state = read_state(db, session_id)
    if state.applied.number == 0:
        raise PlanError("no plan is applied, so there is nothing to undo")
    previous = read_version(db, session_id, state.applied.parent)
    set_pending(db, session_id, state.applied.number, previous.number, requested=True, now=now)
    return previous


def promote(db: Connection, session_id: str, version: int, now: datetime) -> None:
    """Make the pending `version` the applied one (a no-op if someone else already did)."""
    db.execute(
        update(PlanStateRecord)
        .where(PlanStateRecord.session_id == session_id, PlanStateRecord.pending_version == version)
        .values(applied_version=version, pending_version=None, apply_requested=False, updated_at=now)
    )


def set_pending(db: Connection, session_id: str, applied: int, pending: int, requested: bool, now: datetime) -> None:
    values = {"applied_version": applied, "pending_version": pending, "apply_requested": requested, "updated_at": now}
    db.execute(
        sqlite_insert(cast(Table, PlanStateRecord.__table__))
        .values(session_id=session_id, **values)
        .on_conflict_do_update(index_elements=["session_id"], set_=values)
    )


# --- One Store transaction each ------------------------------------------------------------------


async def load_state(store: Store, session_id: str) -> PlanState:
    return await store.run(lambda db: read_state(db.connection(), session_id))


async def create(store: Store, session_id: str, operations: tuple[Operation, ...]) -> PlanVersion:
    now = datetime.now(UTC)
    return await store.run(lambda db: create_version(db.connection(), session_id, operations, now), immediate=True)


async def apply(store: Store, session_id: str) -> PlanVersion:
    now = datetime.now(UTC)
    return await store.run(lambda db: request_apply(db.connection(), session_id, now), immediate=True)


async def undo(store: Store, session_id: str) -> PlanVersion:
    now = datetime.now(UTC)
    return await store.run(lambda db: request_undo(db.connection(), session_id, now), immediate=True)


async def promote_pending(store: Store, session_id: str, version: int) -> None:
    """On the request path: as short a wait for the write lock as identification's (§3 budget)."""
    now = datetime.now(UTC)
    try:
        await store.run(
            lambda db: promote(db.connection(), session_id, version, now),
            busy_timeout_ms=SQLITE_BUSY_TIMEOUT_MS,
            immediate=True,
        )
    except OperationalError as exc:
        if "locked" in str(exc.orig) or "busy" in str(exc.orig):
            raise StoreBusy("the database is locked by another writer") from exc
        raise
