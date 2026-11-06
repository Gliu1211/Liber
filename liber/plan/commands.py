"""`liber plan create|apply|undo|show`: the user's side of edit plans until the dashboard (§10).

Each command works on the same database as the running proxy (SQLite allows that; the proxy's
busy timeouts cover the moment a command writes). Each returns the text to print, so tests can
call them directly; liber/cli.py only parses arguments.

Items are named by their ids, as `show` lists them. Summaries are given as "through message N":
the range starts where the live region starts and must end just before a user message that
starts a new exchange (§5); the request path checks the same rules again (plan/apply.py).

A summary's text comes from the background worker's per-exchange summaries (§9,
cache/exchanges.py). If one of the exchanges isn't summarized yet, `create` refuses and says so
(owner decision, Session 5): a plan never gets a placeholder in place of a summary.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from liber.cache import exchanges
from liber.config import DEFAULT_PRICING, DEFAULT_SUMMARY_MODEL, ModelPrice
from liber.model.items import ExchangeSummary, Operation, OperationKind
from liber.plan import cost, versions
from liber.plan.apply import summarize_operation
from liber.plan.versions import PlanError
from liber.session.store import BranchRecord, ChainNode, ItemRecord, RequestRecord, Store
from liber.worker.summarize import summarizer_id


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


async def show(
    store: Store,
    session_id: str | None = None,
    pricing: dict[str, ModelPrice] | None = None,
    summary_model: str = DEFAULT_SUMMARY_MODEL,
) -> str:
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
    model = await store.run(lambda db: latest_model(db, session_id))
    done = exchanges.completed_exchanges(items)
    keys = [exchange.key for exchange in done]

    def read(db: Session) -> tuple[dict[str, int], dict[str, ExchangeSummary]]:
        exact = exchanges.cached_tokens(db.connection(), keys, model) if model else {}
        return exact, exchanges.cached_summaries(db.connection(), keys, summarizer_id(summary_model))

    exact, summaries = await store.run(read)
    lines = [f"Session {session_id}", f"  applied plan version: {state.applied.number}"]
    lines.append(
        f"  completed exchanges: {len(done)}; summarized: {sum(key in summaries for key in keys)}; "
        f"exact token counts: {sum(key in exact for key in keys)}"
    )
    if state.pending is not None:
        when = "at the next clean turn" if state.apply_requested else "not requested yet (liber plan apply)"
        lines.append(f"  pending plan version: {state.pending.number}, applies {when}")
    for version in (state.applied, state.pending):
        if version is not None and version.number:
            lines.append(f"  version {version.number} (built on {version.parent}):")
            lines += [f"    {op.kind} {len(op.item_ids)} item(s)" for op in version.plan.operations]
    if state.pending is not None:
        price = cost.price_for(model, pricing if pricing is not None else DEFAULT_PRICING)
        counted = cost.with_exact_counts(items, done, exact)
        estimate = cost.estimate(counted, state.applied.plan, state.pending.plan, price, state.activated_at)
        lines += describe_estimate(estimate, exact_exchanges=sum(key in exact for key in keys))
    lines.append("  msg block role       kind         tokens  id")
    lines += [f"  {i.msg_index:>3} {i.block_index:>5} {i.role:10} {i.kind:12} {i.tokens:>6}  {i.id}" for i in items]
    return "\n".join(lines)


def latest_model(db: Session, session_id: str) -> str | None:
    """The model of the session's most recent recorded request (for its prices)."""
    query = select(RequestRecord.model).where(RequestRecord.session_id == session_id).order_by(RequestRecord.id.desc())
    return db.scalars(query).first()


def describe_estimate(estimate: cost.Estimate, exact_exchanges: int = 0) -> list[str]:
    """What applying the pending version costs and saves (§6), for `show`."""
    kind = "minor (appends after the frozen blocks)" if estimate.minor else "major (rewrites earlier history)"
    source = (
        f"exact counts for {exact_exchanges} exchange(s), estimates elsewhere" if exact_exchanges else "token estimates"
    )
    lines = [
        f"  applying the pending version: {kind} (estimates, from {source})",
        f"    tokens per request: about {estimate.tokens_now:,} now, {estimate.tokens_after:,} after",
        f"    reprocessed once: about {estimate.reprocessed_tokens:,} tokens",
        f"    reasoning dropped: {estimate.dropped_thinking_blocks} thinking blocks "
        f"(about {estimate.dropped_thinking_tokens:,} tokens), left out from then on",
    ]
    if estimate.one_time_cost is None or estimate.saving_per_turn is None:
        lines.append("    estimated cost: no price for this session's model (see LIBER_PRICING)")
    else:
        payback = f"{estimate.payback_turns} turns" if estimate.payback_turns is not None else "never (it adds tokens)"
        lines.append(
            f"    estimated cost: ${estimate.one_time_cost:.4f} once, saves ${estimate.saving_per_turn:.4f} per turn; "
            f"pays back after {payback}"
        )
    return lines


async def create(
    store: Store,
    session_id: str,
    summarize_through: int | None = None,
    offload: tuple[str, ...] = (),
    delete: tuple[str, ...] = (),
    note: str | None = None,
    summary_model: str = DEFAULT_SUMMARY_MODEL,
    carry_forward_reasoning: bool = False,
) -> str:
    """Make a new pending plan version: the newest version plus these edits.

    One transaction (BEGIN IMMEDIATE): two commands run at once can't both build on the same
    version and number their summaries alike.
    """
    now = datetime.now(UTC)
    writer = SummaryWriter(summarizer_id(summary_model), carry_forward_reasoning)
    version = await store.run(
        lambda db: create_now(db, session_id, summarize_through, offload, delete, note, now, writer), immediate=True
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
    writer: "SummaryWriter",
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
        operations.append(summary_through(db, state, items, summarize_through, writer))
    if offload:
        operations.append(Operation(kind=OperationKind.OFFLOAD, item_ids=offload, note=note))
    if delete:
        operations.append(Operation(kind=OperationKind.DELETE, item_ids=delete))
    if not operations:
        raise PlanError("nothing to do: give --summarize-through, --offload or --delete")
    return versions.create_version(connection, session_id, tuple(operations), now)


@dataclass(frozen=True)
class SummaryWriter:
    """Which of the worker's summaries a new plan version uses."""

    summarizer: str  # summarizer_id(summary model)
    carry_forward_reasoning: bool  # include the notes (and wait for them)


def summary_through(
    db: Session, state: versions.PlanState, items: list[ItemRecord], last: int, writer: SummaryWriter
) -> Operation:
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
    in_range = [e for e in exchanges.completed_exchanges(items) if first <= e.first and e.last <= last]
    written = exchanges.cached_summaries(db.connection(), [e.key for e in in_range], writer.summarizer)
    missing = [
        e for e in in_range if e.key not in written or (writer.carry_forward_reasoning and written[e.key].notes is None)
    ]
    if missing:
        ranges = ", ".join(f"{e.first}-{e.last}" for e in missing)
        raise PlanError(
            f"not ready: the summaries of messages {ranges} aren't written yet. The background worker "
            "writes them while the proxy runs (it needs LIBER_WORKER_API_KEY); try again in a minute."
        )
    chosen = [written[e.key] for e in in_range]
    if not writer.carry_forward_reasoning:
        chosen = [replace(summary, notes=None) for summary in chosen]  # notes only with the setting on
    return summarize_operation(items, first, last, block_number=len(summaries) + 1, summaries=chosen)


async def apply(store: Store, session_id: str) -> str:
    version = await versions.apply(store, session_id)
    return f"Plan version {version.number} will be applied at the session's next request that starts a new turn."


async def undo(store: Store, session_id: str) -> str:
    version = await versions.undo(store, session_id)
    return f"Going back to plan version {version.number} at the session's next request that starts a new turn."
