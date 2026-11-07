"""What the dashboard shows, worked out from plan/commands.SessionView (ARCHITECTURE.md §10).

Plain functions from data to rows, so the templates only lay things out and the numbers can be
tested on their own. Nothing here changes anything: actions go through plan/commands.py.

- `item_rows`: the item table (role, kind, tokens, topic, what the applied and pending versions
  do to each item).
- `topic_map`: items grouped by topic with token totals. Topics are the worker's, one per
  completed exchange (ADR 0013); the turn in progress has none yet.
- `summary_ends`: the messages a new summary can end at (§5 boundaries), for the summarize form.
- `usage_chart`: input tokens, cache reads and cache writes per request, as SVG polylines.
"""

from dataclasses import dataclass
from datetime import datetime

from liber.model.items import Plan
from liber.plan import commands, cost
from liber.plan.commands import SessionView
from liber.session.store import RequestRecord

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
                exact=item.id in exact_items and item.kind != "thinking",
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
    """(last message, label) for each exchange a new summary could end with (§5)."""
    first = commands.live_start(view.state, view.items)
    ends = []
    for exchange in view.exchanges:
        if exchange.last >= first:
            summary = view.summaries.get(exchange.key)
            label = summary.topic if summary is not None else NOT_SUMMARIZED
            ends.append((exchange.last, f"through message {exchange.last}: {label}"))
    return ends


@dataclass(frozen=True)
class Chart:
    width: int
    height: int
    series: list[tuple[str, str]]  # (name, SVG points "x,y x,y ...")
    largest: int  # tokens at the top of the chart
    requests: int
    first: datetime | None
    last: datetime | None


CHART_SERIES = (
    ("uncached input", "input_tokens"),
    ("cache reads", "cache_read_input_tokens"),
    ("cache writes", "cache_creation_input_tokens"),
)


def usage_chart(requests: list[RequestRecord], width: int = 640, height: int = 160) -> Chart:
    """Per request, oldest to newest: the three kinds of input tokens the API reported (D9, §7)."""
    with_usage = [r for r in requests if r.input_tokens is not None]
    values = {name: [getattr(r, field) or 0 for r in with_usage] for name, field in CHART_SERIES}
    largest = max([max(points, default=0) for points in values.values()] + [1])
    step = width / max(len(with_usage) - 1, 1)
    series = []
    for name, _ in CHART_SERIES:
        points = " ".join(
            f"{index * step:.1f},{height - value / largest * height:.1f}" for index, value in enumerate(values[name])
        )
        series.append((name, points))
    return Chart(
        width,
        height,
        series,
        largest,
        len(with_usage),
        with_usage[0].created_at if with_usage else None,
        with_usage[-1].created_at if with_usage else None,
    )


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
            described = [
                f"{op.kind} {len(op.item_ids)} item(s)" + (f' — "{op.note}"' if op.note else "")
                for op in version.plan.operations
            ]
            versions.append((version.number, version.parent, described))
    return versions
