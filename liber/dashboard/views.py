"""What the dashboard shows, worked out from plan/commands.SessionView (ARCHITECTURE.md §10).

Plain functions from data to rows, so the templates only lay things out and the numbers can be
tested on their own. Nothing here changes anything: actions go through plan/commands.py.

- `item_rows`: the item table (role, kind, tokens, topic, what the applied and pending versions
  do to each item).
- `topic_map`: items grouped by topic with token totals. Topics are the worker's, one per
  completed exchange (ADR 0013); the turn in progress has none yet.
- `summary_ends`: the messages a new summary can end at (§5 boundaries), for the summarize form.
- `usage_chart`: input tokens, cache reads and cache writes per request, as SVG polylines, with
  a mark per request for what Liber did with it.
- `context_meter`: how much of the model's context window the latest request filled, as Liber
  sent it, and about how much it would have filled without Liber (docs/decisions/0019).
- `side_branches`: branches whose last message was replaced, folded away in the session list.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from liber.model.items import Plan
from liber.plan import commands, cost, policy
from liber.plan.commands import SessionView
from liber.plan.versions import PlanVersion
from liber.session.store import BranchRecord, RequestRecord

NOT_SUMMARIZED = "not summarized yet"
CURRENT_TURN = "current turn"


@dataclass(frozen=True)
class ItemRow:
    id: str
    msg_index: int
    block_index: int
    role: str
    kind: str
    tokens: int
    exact: bool  # tokens come from the exchange's exact count (D9)
    topic: str
    status: str  # what the applied version does to it
    pending: str | None  # what the pending version would do instead, if different
    selectable: bool  # can be offloaded or deleted (thinking never is, §5)


@dataclass(frozen=True)
class TopicGroup:
    topic: str
    first: int  # messages
    last: int
    items: int
    tokens: int


def counted_items(view: SessionView) -> list[cost.ItemFacts]:
    """The items' tokens, exact where the worker counted their exchange."""
    return cost.with_exact_counts(view.items, view.exchanges, view.exact)


def item_rows(view: SessionView) -> list[ItemRow]:
    topics = topics_by_item(view)
    exact_items = {item.id for exchange in view.exchanges if exchange.key in view.exact for item in exchange.items}
    tokens = {item.id: item.tokens for item in counted_items(view)}
    applied = what_plan_does(view.state.applied.plan, view.state.applied.number)
    pending = (
        what_plan_does(view.state.pending.plan, view.state.pending.number) if view.state.pending is not None else None
    )
    rows = []
    for item in view.items:
        status = applied.get(item.id, "kept")
        change = pending.get(item.id, "kept") if pending is not None else None
        rows.append(
            ItemRow(
                id=item.id,
                msg_index=item.msg_index,
                block_index=item.block_index,
                role=item.role,
                kind=item.kind,
                tokens=tokens[item.id],
                # Exact counts leave out thinking and system messages: those stay estimates.
                exact=item.id in exact_items and item.kind != "thinking" and item.role != "system",
                topic=topics.get(item.id, CURRENT_TURN),
                status=status,
                pending=change if change != status else None,
                selectable=item.kind != "thinking",
            )
        )
    return rows


def topics_by_item(view: SessionView) -> dict[str, str]:
    topics = {}
    for exchange in view.exchanges:
        summary = view.summaries.get(exchange.key)
        for item in exchange.items:
            topics[item.id] = summary.topic if summary is not None else NOT_SUMMARIZED
    return topics


def what_plan_does(plan: Plan, number: int) -> dict[str, str]:
    """Each item a plan touches -> e.g. "summarized (v2)". The last operation naming it wins."""
    done = {}
    verbs = {"summarize": "summarized", "offload": "offloaded", "delete": "deleted"}
    for operation in plan.operations:
        for item_id in operation.item_ids:
            done[item_id] = f"{verbs.get(operation.kind, operation.kind)} (v{number})"
    return done


def topic_map(view: SessionView) -> list[TopicGroup]:
    """One group per completed exchange, oldest first, then the turn in progress."""
    tokens = {item.id: item.tokens for item in counted_items(view)}
    groups = []
    grouped: set[str] = set()
    for exchange in view.exchanges:
        summary = view.summaries.get(exchange.key)
        topic = summary.topic if summary is not None else NOT_SUMMARIZED
        total = sum(tokens[item.id] for item in exchange.items)
        groups.append(TopicGroup(topic, exchange.first, exchange.last, len(exchange.items), total))
        grouped.update(item.id for item in exchange.items)
    rest = [item for item in view.items if item.id not in grouped]
    if rest:
        total = sum(tokens[item.id] for item in rest)
        groups.append(TopicGroup(CURRENT_TURN, rest[0].msg_index, rest[-1].msg_index, len(rest), total))
    return groups


def summary_ends(view: SessionView) -> list[tuple[int, str]]:
    """(last message, label) for each place a new summary could end (the checks `create` makes, §5)."""
    first = commands.live_start(view.state, view.items)
    allowed = commands.exchange_ends(view.items)
    ends = []
    for exchange in view.exchanges:
        if exchange.last >= first and exchange.last in allowed:
            summary = view.summaries.get(exchange.key)
            label = summary.topic if summary is not None else NOT_SUMMARIZED
            ends.append((exchange.last, f"through message {exchange.last}: {label}"))
    return ends


@dataclass(frozen=True)
class Mark:
    """What Liber did with one request: a dot at the bottom of the chart, at its x position."""

    x: float
    outcome: str  # "rendered", "passthrough", "fallback" or "unknown" (recorded before Liber said)
    title: str  # shown on hover


@dataclass(frozen=True)
class Chart:
    width: int
    height: int
    series: list[tuple[str, str]]  # (name, SVG points "x,y x,y ...")
    largest: int  # tokens at the top of the chart
    requests: int
    first: datetime | None
    last: datetime | None
    marks: tuple[Mark, ...] = ()


CHART_SERIES = (
    ("uncached input", "input_tokens"),
    ("cache reads", "cache_read_input_tokens"),
    ("cache writes", "cache_creation_input_tokens"),
)


def usage_chart(requests: list[RequestRecord], width: int = 640, height: int = 160, top: int = 8) -> Chart:
    """Per request, oldest to newest: the three kinds of input tokens the API reported (D9, §7)."""
    with_usage = [r for r in requests if r.input_tokens is not None]  # e.g. not failed requests
    values = {name: [getattr(r, field) or 0 for r in with_usage] for name, field in CHART_SERIES}
    # The largest value of any series sits `top` pixels below the top edge (never 0: no division by 0).
    largest = max([max(points, default=0) for points in values.values()] + [1])
    # Requests are spread evenly from the left edge to the right one.
    step = width / max(len(with_usage) - 1, 1)
    series = []
    for name, _ in CHART_SERIES:
        coordinates = [
            (index * step, height - value / largest * (height - top)) for index, value in enumerate(values[name])
        ]
        if len(coordinates) == 1:  # one request: a flat line across, since one point draws nothing
            coordinates.append((width, coordinates[0][1]))
        series.append((name, " ".join(f"{x:.1f},{y:.1f}" for x, y in coordinates)))
    marks = tuple(
        Mark(index * step, r.liber_outcome or "unknown", f"{r.created_at:%H:%M:%S}: {r.liber_outcome or 'unknown'}")
        for index, r in enumerate(with_usage)
    )
    return Chart(
        width,
        height,
        series,
        largest,
        len(with_usage),
        with_usage[0].created_at if with_usage else None,
        with_usage[-1].created_at if with_usage else None,
        marks,
    )


# --- How much of the context window is used (docs/decisions/0019) ------------------------------

# Squares in the meter's grid, as Claude Code's /context draws them: 10 x 10 for a 200k window,
# 20 x 10 for a 1M one. Each square is a fixed share of the window.
SMALL_GRID = (10, 100)  # (columns, squares)
LARGE_GRID = (20, 200)
LARGE_WINDOW = 500_000  # windows at least this big get the large grid


@dataclass(frozen=True)
class MeterPart:
    """One line of the meter's legend, and the squares it fills."""

    name: str
    css: str  # the class that colours its squares
    tokens: int
    estimate: bool  # from Liber's estimates, not the API's count


@dataclass(frozen=True)
class ContextMeter:
    model: str | None
    window: int | None  # None: the model isn't in policy.CONTEXT_WINDOWS
    used: int  # exact: the latest request's input as the API counted it (uncached + cache reads + writes)
    outcome: str | None  # what Liber did with that request
    saved: int | None  # estimated message tokens Liber left out of it; 0: sent as is; None: unknown
    plan_version: int | None = None  # the version it was sent with
    parts: tuple[MeterPart, ...] = ()  # the legend, in grid order; empty when not broken down
    squares: tuple[str, ...] = ()  # each square's class, in order
    columns: int = 0

    @property
    def left(self) -> int | None:
        return max(self.window - self.used, 0) if self.window is not None else None

    @property
    def without_liber(self) -> int | None:
        return self.used + self.saved if self.saved is not None else None


def input_tokens(record: RequestRecord) -> int:
    """All of a request's input, as the API counted it: uncached, read from the cache, written to it."""
    return (
        (record.input_tokens or 0) + (record.cache_read_input_tokens or 0) + (record.cache_creation_input_tokens or 0)
    )


def latest_with_usage(requests: Sequence[RequestRecord]) -> RequestRecord | None:
    """The newest request the API reported usage for (a failed one has none)."""
    return next((r for r in reversed(requests) if r.input_tokens is not None), None)


def context_meter(
    latest: RequestRecord | None,
    applied: PlanVersion,
    items: Sequence[cost.ItemFacts] | None = None,
) -> ContextMeter | None:
    """How full the context window was on the latest request, as Liber sent it, and without Liber.

    `items`: the session's items (exact counts where the worker has them). Without them, only the
    totals: the session list leaves out the breakdown, and the saving for requests Liber edited.

    - Used is the API's count, so it is exact, whatever Liber did.
    - The saving is an estimate: the message tokens the applied plan takes out, as in a plan's
      preview (plan/cost.py). Thinking isn't counted on either side, so stale thinking Liber left
      out isn't in it: the real saving is at least this. A plan that grew the messages (§6 refuses
      those when they're made) shows no saving. A request sent unchanged saved nothing.
    - The saving and the breakdown are only worked out for a request sent with the applied
      version: after an undo, the plan it was sent with isn't the one at hand, so only the totals.
    - The breakdown estimates the messages as sent (kept items, then Liber's summaries and pointer
      notes). The rest of the API's count is the system prompt, the tools and the thinking, which
      Liber doesn't estimate apart.
    """
    if latest is None:
        return None
    used = input_tokens(latest)
    window = policy.context_window(latest.model)
    outcome = latest.liber_outcome
    thinking = {item.id for item in items or () if item.kind == "thinking"}
    edited = outcome == "rendered"
    # Sent unchanged (a fallback too: its messages are the client's), or with the applied version.
    known = not edited or latest.plan_version == applied.number
    sent_plan = applied.plan if edited else Plan()
    saved: int | None
    if not edited:
        saved = 0
    elif items is None or not known:
        saved = None  # sent with another version (an undo since), or not worked out here
    else:
        without = cost.rendered_tokens(list(items), Plan(), thinking)
        saved = max(without - cost.rendered_tokens(list(items), sent_plan, thinking), 0)
    meter = ContextMeter(latest.model, window, used, outcome, saved, latest.plan_version)
    if window is None or items is None or not known:
        return meter
    summaries = cost.frozen_tokens(sent_plan)
    messages = cost.rendered_tokens(list(items), sent_plan, thinking) - summaries
    parts = [
        MeterPart("System prompt, tools and thinking", "rest", max(used - messages - summaries, 0), False),
        MeterPart("Messages", "messages", messages, True),
        MeterPart("Liber's summaries", "summaries", summaries, True),
        # Part of the free space: drawn before the rest of it, so the grid shows how much Liber made.
        MeterPart("Freed by Liber (part of the free space)", "freed", max(saved or 0, 0), True),
        MeterPart("Free space", "free", max(window - used, 0), False),
    ]
    columns, count = LARGE_GRID if window >= LARGE_WINDOW else SMALL_GRID
    return replace(meter, parts=tuple(parts), squares=grid(parts, window, count), columns=columns)


def grid(parts: Sequence[MeterPart], window: int, count: int) -> tuple[str, ...]:
    """Each square's class: the parts in order, each up to where its running total reaches.

    Rounding the running total (not each part) keeps the squares adding up to the grid; a part
    smaller than half a square gets none, as in Claude Code's grid. Anything past the window is
    cut: the freed space is also counted in the free space, which comes last.
    """
    squares: list[str] = []
    running = 0
    for part in parts:
        running += part.tokens
        end = min(round(running / window * count), count)
        squares += [part.css] * max(end - len(squares), 0)
    squares += ["free"] * (count - len(squares))
    return tuple(squares)


def compact(tokens: int | None) -> str:
    """Token counts as Claude Code shows them: 950, 69.3k, 1.0M."""
    if tokens is None:
        return "?"
    if abs(tokens) < 1_000:
        return str(tokens)
    if abs(tokens) < 1_000_000:
        return f"{tokens / 1_000:.1f}k"
    return f"{tokens / 1_000_000:.1f}M"


def percent(tokens: int | None, window: int | None) -> str:
    if tokens is None or not window:
        return ""
    return f"{tokens / window:.1%}"


def estimate_lines(view: SessionView) -> list[str]:
    """The §6 preview of applying the pending version, worded as `liber plan show` words it."""
    if view.estimate is None:
        return []
    return [line.strip() for line in commands.describe_estimate(view.estimate, view.exact_exchanges)]


def plan_lines(view: SessionView) -> list[tuple[int, int, list[str]]]:
    """(version, built on, its operations described) for the applied and pending versions."""
    versions = []
    for version in (view.state.applied, view.state.pending):
        if version is not None and version.number:
            described = [commands.describe_operation(op) for op in version.plan.operations]
            versions.append((version.number, version.parent, described))
    return versions


@dataclass(frozen=True)
class NodeFacts:
    """What side_branches needs from a stored message (session/store.ChainNode)."""

    hash: str
    parent_hash: str
    session_id: str
    branch_id: str  # the branch that first contained the message
    created_at: datetime


def side_branches(branches: list[BranchRecord], nodes: list[NodeFacts]) -> set[str]:
    """Ids of branches that end in a message that was later replaced.

    Such a branch's newest message has no continuation, and a later message was sent in its
    place (same parent). In Claude Code that is mostly its "welcome back" recap: when you return
    after a break, it sends your conversation plus a recap prompt as a side request, and your
    next real message forks again at the same point. A one-message rewind looks the same, and is
    equally finished. A branch that diverged further back is a real alternative and stays listed.

    `nodes` must hold each branch's head, and every child of a head or of a head's parent.

    - Siblings are counted per session: first messages share the root hash across sessions
      (same system prompt and tools), but they are different sessions, not replacements.
    - "Later" means the sibling was used after this branch was last used: when the sibling was
      first sent, or its branch's last use. So a branch the user went back to (rewind, then resend
      the same message) is current again, and the other one is folded.
    - A tie on time folds nothing.
    """
    has_child = {node.parent_hash for node in nodes}
    by_hash = {node.hash: node for node in nodes}
    last_used = {branch.id: branch.last_seen_at for branch in branches}
    side = set()
    for branch in branches:
        head = by_hash.get(branch.head_hash)
        if head is None or head.hash in has_child:
            continue  # unknown, or continued
        for sibling in nodes:
            if sibling.hash == head.hash or (sibling.session_id, sibling.parent_hash) != (
                head.session_id,
                head.parent_hash,
            ):
                continue
            used = max(sibling.created_at, last_used.get(sibling.branch_id, sibling.created_at))
            if used > branch.last_seen_at:
                side.add(branch.id)  # a later message replaced this branch's last one
                break
    return side
