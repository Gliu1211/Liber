"""`liber plan create|apply|undo|show`: the user's side of edit plans (§4, §10).

The dashboard (liber/dashboard/) calls these same functions, so a plan made in the browser goes
through exactly the checks a plan made on the command line does.

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

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from liber.cache import exchanges
from liber.config import DEFAULT_PRICING, DEFAULT_SUMMARY_MODEL, ModelPrice
from liber.model.anthropic import parse
from liber.model.items import ExchangeSummary, Operation, OperationKind, Plan
from liber.plan import cost, versions
from liber.plan.apply import CODE_HEADING, PINNABLE, Pinned, pinned_text, summarize_operation
from liber.plan.versions import PlanError
from liber.session.store import BranchRecord, ChainNode, ItemRecord, RequestRecord, Store
from liber.worker.summarize import summarizer_id

log = structlog.get_logger(__name__)


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


@dataclass(frozen=True)
class SessionView:
    """Everything `show` and the dashboard (§10) know about one session, read in one place."""

    session_id: str
    items: list[ItemRecord]  # the latest branch's, in conversation order
    state: versions.PlanState
    model: str | None  # of the session's latest recorded request
    exchanges: list[Any]  # cache/exchanges.Exchange of ItemRecord: the completed ones, oldest first
    summaries: dict[str, ExchangeSummary]  # the worker's, by exchange key
    exact: dict[str, int]  # the worker's exact token counts, by exchange key
    estimate: cost.Estimate | None  # what applying the pending version costs and saves

    @property
    def exact_exchanges(self) -> int:
        return sum(exchange.key in self.exact for exchange in self.exchanges)


async def session_view(
    store: Store,
    session_id: str,
    pricing: dict[str, ModelPrice] | None = None,
    summary_model: str = DEFAULT_SUMMARY_MODEL,
) -> SessionView:
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
    estimate = None
    if state.pending is not None:
        price = cost.price_for(model, pricing if pricing is not None else DEFAULT_PRICING)
        counted = cost.with_exact_counts(items, done, exact)
        estimate = cost.estimate(counted, state.applied.plan, state.pending.plan, price, state.activated_at)
    return SessionView(session_id, items, state, model, done, summaries, exact, estimate)


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

    view = await session_view(store, session_id, pricing, summary_model)
    state, keys = view.state, [exchange.key for exchange in view.exchanges]
    lines = [f"Session {session_id}", f"  applied plan version: {state.applied.number}"]
    lines.append(
        f"  completed exchanges: {len(keys)}; summarized: {sum(key in view.summaries for key in keys)}; "
        f"exact token counts: {view.exact_exchanges}"
    )
    if state.pending is not None:
        when = "at the next clean turn" if state.apply_requested else "not requested yet (liber plan apply)"
        lines.append(f"  pending plan version: {state.pending.number}, applies {when}")
    for version in (state.applied, state.pending):
        if version is not None and version.number:
            lines.append(f"  version {version.number} (built on {version.parent}):")
            lines += [f"    {describe_operation(op)}" for op in version.plan.operations]
    if view.estimate is not None:
        lines += describe_estimate(view.estimate, exact_exchanges=view.exact_exchanges)
    lines.append("  msg block role       kind         tokens  id")
    lines += [
        f"  {i.msg_index:>3} {i.block_index:>5} {i.role:10} {i.kind:12} {i.tokens:>6}  {i.id}" for i in view.items
    ]
    return "\n".join(lines)


def describe_operation(operation: Operation) -> str:
    """One line per operation, for `show` and the dashboard."""
    note = f' — "{operation.note}"' if operation.note else ""
    pins = f", keeping {len(operation.pinned_item_ids)} as is" if operation.pinned_item_ids else ""
    return f"{operation.kind} {len(operation.item_ids)} item(s){pins}{note}"


def exchange_ends(items: list[ItemRecord]) -> set[int]:
    """Messages a summary may end at: each one just before a user message that starts an exchange (§5)."""
    starts = {i.msg_index for i in items if i.role == "user"} - {i.msg_index for i in items if i.kind == "tool_result"}
    return {start - 1 for start in starts if start > 0}


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
        f"    tokens per request (thinking not counted): about {estimate.tokens_now:,} now, "
        f"{estimate.tokens_after:,} after",
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
    pin: tuple[str, ...] = (),
) -> str:
    """Make a new pending plan version: the newest version plus these edits.

    `pin`: items inside the summarized range to keep as they were (docs/decisions/0016).

    One transaction (BEGIN IMMEDIATE): two commands run at once can't both build on the same
    version and number their summaries alike.
    """
    now = datetime.now(UTC)
    pin = tuple(dict.fromkeys(pin))  # the same item twice is kept once
    # Pinned text is read before the write transaction: parsing recorded bodies can take a while,
    # and the proxy must not wait on the write lock meanwhile (hard rule 1).
    blocks = await store.run(lambda db: blocks_to_pin(db, session_id, pin)) if pin else {}
    writer = SummaryWriter(summarizer_id(summary_model), carry_forward_reasoning, pin, blocks)
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
    unknown = [item_id for item_id in offload + delete + writer.pin if item_id not in by_id]
    if unknown:
        raise PlanError(f"no such item in the session's latest branch: {', '.join(unknown)}")
    thinking = [item_id for item_id in offload + delete if by_id[item_id].kind == "thinking"]
    if thinking:  # §5, hard rule 6: the request path would refuse the version, so refuse it now
        raise PlanError(f"thinking blocks are never offloaded or deleted: {', '.join(thinking)}")
    if writer.pin and summarize_through is None:
        raise PlanError("--pin keeps items through a summary: give --summarize-through too")
    both = sorted(set(writer.pin) & set(offload + delete))
    if both:
        raise PlanError(f"an item can't be both kept and offloaded or deleted: {', '.join(both)}")

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
    refuse_unless_smaller(db, session_id, items, state, tuple(operations))
    return versions.create_version(connection, session_id, tuple(operations), now)


def refuse_unless_smaller(
    db: Session, session_id: str, items: list[ItemRecord], state: versions.PlanState, operations: tuple[Operation, ...]
) -> None:
    """Refuse a version whose requests wouldn't be smaller than what is sent now (owner, 2026-11-06).

    Every compaction costs something once: the prompt cache restarts from the edit and older
    reasoning is dropped (ADR 0012). One that doesn't shrink the request pays that and gains
    nothing, e.g. a summary longer than the short exchanges it replaces, or a pointer note bigger
    than the item it offloads. Uses the same estimate `liber plan show` prints, so the numbers
    in the refusal match what show would say. Thinking isn't counted on either side (plan/cost.py):
    leaving it out isn't a saving the API was shown to give.
    """
    base = state.pending or state.applied  # a new version builds on the newest one
    candidate = Plan(operations=base.plan.operations + operations)
    model = latest_model(db, session_id)
    done = exchanges.completed_exchanges(items)
    exact = exchanges.cached_tokens(db.connection(), [e.key for e in done], model) if model else {}
    counted = cost.with_exact_counts(items, done, exact)
    # Compared with the applied version: that is what requests send today (a pending one isn't sent).
    estimate = cost.estimate(counted, state.applied.plan, candidate, None, state.activated_at)
    candidate_text = "".join(op.summary or "" for op in operations)
    if estimate.tokens_after >= estimate.tokens_now:
        raise PlanError(
            f"this plan wouldn't make the conversation smaller (about {estimate.tokens_now:,} tokens per "
            f"request now, {estimate.tokens_after:,} after, thinking not counted), so it isn't worth the "
            "cache restart. "
            + ("Kept code is part of the summary and counted too. " if CODE_HEADING in candidate_text else "")
            + "Summarize a longer range, or offload or delete larger items."
        )


def live_start(state: versions.PlanState, items: list[ItemRecord]) -> int:
    """Where a new summary would start: after the last message the newest version summarizes."""
    base = state.pending or state.applied
    by_id = {item.id: item for item in items}
    summaries = [op for op in base.plan.operations if op.kind is OperationKind.SUMMARIZE]
    covered = [by_id[item_id].msg_index for op in summaries for item_id in op.item_ids if item_id in by_id]
    return max(covered, default=-1) + 1


@dataclass(frozen=True)
class SummaryWriter:
    """Which of the worker's summaries a new plan version uses."""

    summarizer: str  # summarizer_id(summary model)
    carry_forward_reasoning: bool  # include the notes (and wait for them)
    pin: tuple[str, ...] = ()  # items to keep through the summary
    pin_blocks: dict[str, dict[str, Any]] = field(default_factory=dict)  # their blocks (and their tool calls')


def summary_through(
    db: Session, state: versions.PlanState, items: list[ItemRecord], last: int, writer: SummaryWriter
) -> Operation:
    first = live_start(state, items)
    base = state.pending or state.applied
    summaries = [op for op in base.plan.operations if op.kind is OperationKind.SUMMARIZE]
    if last < first:
        raise PlanError(f"messages up to {first - 1} are already summarized")
    if last not in exchange_ends(items):
        raise PlanError(f"a summary must end just before a user message that starts an exchange; {last + 1} isn't one")
    in_range = [e for e in exchanges.completed_exchanges(items) if first <= e.first and e.last <= last]
    if not in_range:
        raise PlanError(f"messages {first}-{last} hold no completed exchange to summarize")
    written = exchanges.cached_summaries(db.connection(), [e.key for e in in_range], writer.summarizer)
    missing = [
        e for e in in_range if e.key not in written or (writer.carry_forward_reasoning and written[e.key].notes is None)
    ]
    if missing:
        ranges = ", ".join(f"{e.first}-{e.last}" for e in missing)
        raise PlanError(
            f"not ready: the summaries of messages {ranges} aren't written yet. The background worker writes "
            "them while `liber serve` runs with LIBER_WORKER_API_KEY set, usually within a minute of the "
            "session's last request. If this persists, the proxy's log says why (look for worker_off or "
            "worker_gave_up); a summary the worker gave up on is tried again after the proxy restarts. "
            "Meanwhile you can summarize through an earlier message, or offload or delete items instead."
        )
    chosen = [exchanges.resolved(written[e.key], e) for e in in_range]  # kept code tied to this occurrence
    if not writer.carry_forward_reasoning:
        chosen = [replace(summary, notes=None) for summary in chosen]  # notes only with the setting on
    pinned = pinned_items(items, first, last, writer.pin, writer.pin_blocks)
    return summarize_operation(items, first, last, len(summaries) + 1, summaries=chosen, pinned=pinned)


def pinned_items(
    items: list[ItemRecord], first: int, last: int, ids: tuple[str, ...], blocks: dict[str, dict[str, Any]]
) -> list[Pinned]:
    """The items to keep through the summary, with their text as it was (docs/decisions/0016).

    `blocks`: the items' blocks, read from recorded requests beforehand (`blocks_to_pin`).
    """
    if not ids:
        return []
    by_id = {item.id: item for item in items}
    for item_id in ids:
        item = by_id[item_id]
        if not first <= item.msg_index <= last:
            raise PlanError(f"{item_id} is in message {item.msg_index}, outside the summary (messages {first}-{last})")
        if item.kind not in PINNABLE:
            raise PlanError(f"a {item.kind} item can't be kept as text (only text, tool calls and tool results)")
    missing = [item_id for item_id in ids if item_id not in blocks]
    if missing:
        raise PlanError(
            f"can't keep {', '.join(missing)}: Liber finds an item's text only in the session's last "
            f"{RECORDED_REQUESTS_SEARCHED} recorded requests, and none holds it (is LIBER_RECORD_BODIES off?)"
        )
    pinned = []
    for item_id in sorted(ids, key=lambda i: (by_id[i].msg_index, by_id[i].block_index)):  # conversation order
        item = by_id[item_id]
        call = blocks.get(item.depends_on or "")
        name = call.get("name") if call is not None else None
        label, text = pinned_text(
            item.kind, item.role, blocks[item_id], item.msg_index, name if isinstance(name, str) else None
        )
        pinned.append(Pinned(item_id, label, text))
    return pinned


RECORDED_REQUESTS_SEARCHED = 50  # the session's newest requests; the newest normally holds everything


def blocks_to_pin(db: Session, session_id: str, ids: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """The blocks of the items to pin, and of their tool calls (a tool result's label names its tool).

    The text of a conversation is kept only in the recorded request bodies (LIBER_RECORD_BODIES, on
    by default), so that's where it's read from. An item id covers the block's content and everything
    before it, so a block found under that id in any recorded request is the right one.
    """
    items = {item.id: item for item in latest_branch_items(db, session_id)}
    calls = {items[i].depends_on for i in ids if i in items and items[i].depends_on}
    return recorded_blocks(db, session_id, set(ids) | {c for c in calls if c is not None})


def recorded_blocks(db: Session, session_id: str, wanted: set[str]) -> dict[str, dict[str, Any]]:
    """item id -> the client's block, from the session's recorded request bodies, newest first.

    One body in memory at a time: the ids are listed first, then each body is loaded on its own.
    """
    query = (
        select(RequestRecord.id)
        .where(RequestRecord.session_id == session_id, RequestRecord.request_body.is_not(None))
        .order_by(RequestRecord.id.desc())
        .limit(RECORDED_REQUESTS_SEARCHED)
    )
    found: dict[str, dict[str, Any]] = {}
    for request_id in list(db.scalars(query)):
        body = db.scalar(select(RequestRecord.request_body).where(RequestRecord.id == request_id))
        if body is None:
            continue
        try:
            parsed = parse(json.loads(body))
        except Exception as exc:  # a body Liber couldn't parse is no source of blocks
            log.debug("pin_body_unreadable", request_id=request_id, error=type(exc).__name__)
            continue
        found.update({item.id: item.block for item in parsed.items if item.id in wanted and item.id not in found})
        if len(found) == len(wanted):
            break
    return found


async def apply(store: Store, session_id: str) -> str:
    version = await versions.apply(store, session_id)
    return f"Plan version {version.number} will be applied at the session's next request that starts a new turn."


async def undo(store: Store, session_id: str) -> str:
    version = await versions.undo(store, session_id)
    return f"Going back to plan version {version.number} at the session's next request that starts a new turn."
