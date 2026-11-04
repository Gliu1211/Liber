"""`liber plan create|apply|undo|show`: the user's side of edit plans until the dashboard (§10).

Each command works on the same database as the running proxy (SQLite allows that; the proxy's
busy timeouts cover the moment a command writes). Each returns the text to print, so tests can
call them directly; liber/cli.py only parses arguments.

Items are named by their ids, as `show` lists them. Summaries are given as "through message N":
the range starts where the live region starts and must end just before a user message that
starts a new exchange (§5); the request path checks the same rules again (plan/apply.py).
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from liber.model.items import Operation, OperationKind
from liber.plan import versions
from liber.plan.apply import summarize_operation
from liber.plan.versions import PlanError
from liber.session.store import BranchRecord, ChainNode, ItemRecord, Store


def latest_branch_items(db: Session, session_id: str) -> list[ItemRecord]:
    """The items of the session's most recently used branch, in conversation order."""
    branch = db.scalars(
        select(BranchRecord).where(BranchRecord.session_id == session_id).order_by(BranchRecord.last_seen_at.desc())
    ).first()
    if branch is None:
        return []
    rows = db.execute(select(ChainNode.hash, ChainNode.parent_hash).where(ChainNode.session_id == session_id))
    parents = {node_hash: parent_hash for node_hash, parent_hash in rows}
    on_branch = set()
    node = branch.head_hash
    while node in parents:  # walk from the branch's newest message back to its first
        on_branch.add(node)
        node = parents[node]
    items = db.scalars(
        select(ItemRecord)
        .where(ItemRecord.session_id == session_id, ItemRecord.node_hash.in_(on_branch))
        .order_by(ItemRecord.msg_index, ItemRecord.block_index)
    )
    return list(items)


async def show(store: Store, session_id: str | None = None) -> str:
    if session_id is None:
        sessions = await store.list_sessions()
        if not sessions:
            return "No sessions yet."
        lines = ["session                           created              applied  pending"]
        for session in reversed(sessions):  # newest first
            state = await versions.load_state(store, session.id)
            pending = "-"
            if state.pending is not None:
                pending = f"{state.pending.number} (requested)" if state.apply_requested else str(state.pending.number)
            lines.append(f"{session.id}  {session.created_at:%Y-%m-%d %H:%M}  {state.applied.number:>7}  {pending}")
        return "\n".join(lines)

    items = await store.run(lambda db: latest_branch_items(db, session_id))
    if not items:
        raise PlanError(f"no session {session_id}")
    state = await versions.load_state(store, session_id)
    lines = [f"Session {session_id}", f"  applied plan version: {state.applied.number}"]
    if state.pending is not None:
        when = "at the next clean turn" if state.apply_requested else "not requested yet (liber plan apply)"
        lines.append(f"  pending plan version: {state.pending.number}, applies {when}")
    for version in (state.applied, state.pending):
        if version is not None and version.number:
            lines.append(f"  version {version.number} (built on {version.parent}):")
            lines += [f"    {op.kind} {len(op.item_ids)} item(s)" for op in version.plan.operations]
    lines.append("  msg block role       kind         tokens  id")
    lines += [f"  {i.msg_index:>3} {i.block_index:>5} {i.role:10} {i.kind:12} {i.tokens:>6}  {i.id}" for i in items]
    return "\n".join(lines)


async def create(
    store: Store,
    session_id: str,
    summarize_through: int | None = None,
    offload: tuple[str, ...] = (),
    delete: tuple[str, ...] = (),
    note: str | None = None,
) -> str:
    """Make a new pending plan version: the newest version plus these edits.

    One transaction (BEGIN IMMEDIATE): two commands run at once can't both build on the same
    version and number their summaries alike.
    """
    now = datetime.now(UTC)
    version = await store.run(
        lambda db: create_now(db, session_id, summarize_through, offload, delete, note, now), immediate=True
    )
    return f"Created plan version {version.number} (pending). To use it: liber plan apply --session {session_id}"


def create_now(
    db: Session,
    session_id: str,
    summarize_through: int | None,
    offload: tuple[str, ...],
    delete: tuple[str, ...],
    note: str | None,
    now: datetime,
) -> versions.PlanVersion:
    items = latest_branch_items(db, session_id)
    if not items:
        raise PlanError(f"no session {session_id}")
    by_id = {item.id: item for item in items}
    unknown = [item_id for item_id in offload + delete if item_id not in by_id]
    if unknown:
        raise PlanError(f"no such item in the session's latest branch: {', '.join(unknown)}")

    connection = db.connection()
    state = versions.read_state(connection, session_id)
    operations: list[Operation] = []
    if summarize_through is not None:
        operations.append(summary_through(state, items, summarize_through))
    if offload:
        operations.append(Operation(kind=OperationKind.OFFLOAD, item_ids=offload, note=note))
    if delete:
        operations.append(Operation(kind=OperationKind.DELETE, item_ids=delete))
    if not operations:
        raise PlanError("nothing to do: give --summarize-through, --offload or --delete")
    return versions.create_version(connection, session_id, tuple(operations), now)


def summary_through(state: versions.PlanState, items: list[ItemRecord], last: int) -> Operation:
    base = state.pending or state.applied
    by_id = {item.id: item for item in items}
    summaries = [op for op in base.plan.operations if op.kind is OperationKind.SUMMARIZE]
    # The live region starts after the last message the newest version already summarizes.
    covered = [by_id[item_id].msg_index for op in summaries for item_id in op.item_ids if item_id in by_id]
    first = max(covered, default=-1) + 1
    starts = {i.msg_index for i in items if i.role == "user"} - {i.msg_index for i in items if i.kind == "tool_result"}
    if last < first:
        raise PlanError(f"messages up to {first - 1} are already summarized")
    if last + 1 not in starts:
        raise PlanError(f"a summary must end just before a user message that starts an exchange; {last + 1} isn't one")
    return summarize_operation(items, first, last, block_number=len(summaries) + 1)


async def apply(store: Store, session_id: str) -> str:
    version = await versions.apply(store, session_id)
    return f"Plan version {version.number} will be applied at the session's next request that starts a new turn."


async def undo(store: Store, session_id: str) -> str:
    version = await versions.undo(store, session_id)
    return f"Going back to plan version {version.number} at the session's next request that starts a new turn."
