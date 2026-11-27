"""The dashboard's numbers (liber/dashboard/views.py), without a page around them."""

from datetime import UTC, datetime

from liber.dashboard import views
from liber.dashboard.views import usage_chart
from liber.model.items import Operation, OperationKind, Plan
from liber.plan.cost import CountedItem
from liber.plan.versions import EMPTY, PlanVersion
from liber.session.store import BranchRecord, RequestRecord


def request(input_tokens: int | None, cache_read: int = 0, cache_write: int = 0) -> RequestRecord:
    return RequestRecord(
        input_tokens=input_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        created_at=datetime(2026, 11, 7, tzinfo=UTC),
    )


def points(chart_series: str) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y in (pair.split(",") for pair in chart_series.split())]


def test_no_requests_draw_no_lines() -> None:
    chart = usage_chart([])

    assert chart.requests == 0 and all(drawn == "" for _, drawn in chart.series)


def test_one_request_draws_a_flat_line_across() -> None:
    chart = usage_chart([request(100, cache_read=50)], width=640, height=160, top=8)

    by_name = dict(chart.series)
    assert points(by_name["uncached input"]) == [(0.0, 8.0), (640.0, 8.0)]  # the largest: `top` px from the top
    assert points(by_name["cache reads"]) == [(0.0, 84.0), (640.0, 84.0)]  # half of it
    assert points(by_name["cache writes"]) == [(0.0, 160.0), (640.0, 160.0)]  # zero: the bottom


def test_requests_without_usage_are_left_out() -> None:
    chart = usage_chart([request(None), request(10), request(None), request(30)], width=100)

    assert chart.requests == 2
    assert [x for x, _ in points(dict(chart.series)["uncached input"])] == [0.0, 100.0]


# --- Side requests in the session list (ADR 0018) -----------------------------------------------


def at(minute: int) -> datetime:
    return datetime(2026, 11, 23, 22, minute, tzinfo=UTC)


def branch(branch_id: str, head: str, last_used: int, session: str = "s") -> BranchRecord:
    return BranchRecord(
        id=branch_id,
        session_id=session,
        parent_branch_id=None,
        fork_depth=0,
        head_hash=head,
        head_depth=1,
        created_at=at(last_used),
        last_seen_at=at(last_used),
    )


def node(hash_: str, parent: str, minute: int, branch_id: str = "", session: str = "s") -> views.NodeFacts:
    return views.NodeFacts(hash_, parent, session, branch_id, at(minute))


def test_a_branch_whose_last_message_was_replaced_is_a_side_request() -> None:
    """Claude Code's recap: history + recap prompt, then the real next message in its place."""
    nodes = [
        node("m1", "root", 0, "first"),
        node("m2", "m1", 1, "first"),
        node("recap", "m2", 2, "first"),
        node("real", "m2", 3, "main"),
        node("m4", "real", 4, "main"),
    ]
    branches = [branch("first", "recap", 2), branch("main", "m4", 4)]

    assert views.side_branches(branches, nodes) == {"first"}


def test_the_newest_of_two_last_messages_is_not_a_side_request() -> None:
    nodes = [node("m1", "root", 0, "a"), node("older", "m1", 1, "a"), node("newer", "m1", 2, "b")]

    assert views.side_branches([branch("a", "older", 1), branch("b", "newer", 2)], nodes) == {"a"}


def test_a_branch_that_diverged_further_back_stays_listed() -> None:
    """A rewind several messages back is a real alternative, not a side request."""
    nodes = [
        node("m1", "root", 0, "old"),
        node("x2", "m1", 1, "old"),
        node("x3", "x2", 2, "old"),
        node("y2", "m1", 3, "new"),
    ]

    assert views.side_branches([branch("old", "x3", 2), branch("new", "y2", 3)], nodes) == set()


def test_one_message_sessions_under_the_same_system_prompt_are_not_side_requests() -> None:
    """First messages share the root hash across sessions; they don't replace each other."""
    nodes = [node("a1", "root", 0, "A-only", session="A"), node("b1", "root", 1, "B-only", session="B")]
    branches = [branch("A-only", "a1", 0, session="A"), branch("B-only", "b1", 1, session="B")]

    assert views.side_branches(branches, nodes) == set()


def test_a_branch_the_user_went_back_to_is_listed_again() -> None:
    """Rewind and send B, then rewind again and resend A exactly: A's branch is current again."""
    nodes = [node("m1", "root", 0, "A"), node("a", "m1", 1, "A"), node("b", "m1", 2, "B")]

    assert views.side_branches([branch("A", "a", 3), branch("B", "b", 2)], nodes) == {"B"}


def test_ties_and_unknown_heads_fold_nothing() -> None:
    tie = [node("m1", "root", 0, "x"), node("x", "m1", 1, "x"), node("y", "m1", 1, "y")]

    assert views.side_branches([branch("x", "x", 1), branch("y", "y", 1)], tie) == set()
    assert views.side_branches([branch("gone", "missing", 1)], tie) == set()


# --- The context meter (docs/decisions/0019) ----------------------------------------------------


def latest(
    model: str = "claude-opus-5-5",
    outcome: str | None = "passthrough",
    plan_version: int | None = None,
    usage: tuple[int, int, int] = (4, 69_271, 26),
) -> RequestRecord:
    uncached, read, written = usage
    return RequestRecord(
        model=model,
        liber_outcome=outcome,
        plan_version=plan_version,
        input_tokens=uncached,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=written,
        created_at=datetime(2026, 11, 19, tzinfo=UTC),
    )


ITEMS = [
    CountedItem("a", "text", 0, 20_000),
    CountedItem("b", "tool_result", 1, 12_000),
    CountedItem("t", "thinking", 1, 5_000),  # never counted: the estimate includes its signature
    CountedItem("c", "text", 2, 8_000),
]
SUMMARY = "s" * 4_000  # 1,000 tokens by the frozen-block estimate
SUMMARIZED = PlanVersion(
    1, Plan(operations=(Operation(kind=OperationKind.SUMMARIZE, item_ids=("a", "b", "t"), summary=SUMMARY),))
)


def test_a_request_sent_unchanged_uses_the_apis_count_and_saved_nothing() -> None:
    meter = views.context_meter(latest(usage=(2, 99_995, 81)), EMPTY, ITEMS)

    assert meter is not None
    assert meter.used == 100_078 and meter.window == 1_000_000 and meter.left == 899_922
    assert meter.saved == 0 and meter.without_liber == 100_078
    parts = {part.css: part.tokens for part in meter.parts}
    assert parts["messages"] == 40_000 and parts["summaries"] == 0 and parts["freed"] == 0
    assert parts["rest"] == 100_078 - 40_000  # system prompt, tools and thinking: the rest of the count
    assert parts["free"] == 899_922
    # 1M window: 20 x 10 squares, each 0.5% of the window; 10% used is 20 squares.
    assert meter.columns == 20 and len(meter.squares) == 200
    assert meter.squares.count("free") == 180


def test_a_request_liber_edited_shows_what_it_freed() -> None:
    meter = views.context_meter(latest(outcome="rendered", plan_version=1), SUMMARIZED, ITEMS)

    assert meter is not None and meter.used == 69_301
    # a and b (32,000) summarized into 1,000 tokens; thinking isn't counted on either side.
    assert meter.saved == 31_000 and meter.without_liber == 100_301
    parts = {part.css: part.tokens for part in meter.parts}
    assert parts["messages"] == 8_000 and parts["summaries"] == 1_000 and parts["freed"] == 31_000
    assert parts["rest"] == 69_301 - 9_000
    assert parts["free"] == 1_000_000 - 69_301 == meter.left  # the freed space is part of it
    # In order: the rest, messages, summaries, freed, free; the running totals set the boundaries.
    assert [square for square in meter.squares if square != "free"] == (
        ["rest"] * 12 + ["messages"] * 2 + ["freed"] * 6
    )  # 60.3k -> 12, 68.3k -> 14, 69.3k -> 14 (no square for 1k), 100.3k -> 20
    assert len(meter.squares) == 200


def test_the_saving_is_unknown_when_sent_with_another_version_or_without_items() -> None:
    undone = views.context_meter(latest(outcome="rendered", plan_version=1), EMPTY, ITEMS)
    no_items = views.context_meter(latest(outcome="rendered", plan_version=1), SUMMARIZED)

    assert undone is not None and undone.saved is None and undone.without_liber is None
    assert no_items is not None and no_items.saved is None and no_items.parts == () and no_items.squares == ()
    assert no_items.left == 1_000_000 - 69_301  # the totals don't need the items
    # After an undo the plan at hand isn't the one it was sent with: no breakdown from the wrong plan.
    assert undone.parts == () and undone.squares == ()
    no_version = views.context_meter(latest(outcome="rendered", plan_version=None), SUMMARIZED, ITEMS)
    assert no_version is not None and no_version.saved is None and no_version.parts == ()


def test_a_plan_that_grew_the_messages_shows_no_saving() -> None:
    longer = PlanVersion(
        1, Plan(operations=(Operation(kind=OperationKind.SUMMARIZE, item_ids=("c",), summary="s" * 40_000),))
    )
    meter = views.context_meter(latest(outcome="rendered", plan_version=1), longer, ITEMS)

    assert meter is not None and meter.saved == 0 and meter.without_liber == meter.used


def test_a_fallback_saved_nothing_and_a_model_without_a_known_window_has_no_grid() -> None:
    fallback = views.context_meter(latest(outcome="fallback", plan_version=1), SUMMARIZED, ITEMS)
    unknown = views.context_meter(latest(model="some-other-model"), EMPTY, ITEMS)

    assert fallback is not None and fallback.saved == 0
    assert unknown is not None and unknown.window is None and unknown.left is None and unknown.squares == ()
    assert unknown.used == 69_301
    assert views.context_meter(None, EMPTY, ITEMS) is None


def test_a_200k_window_gets_ten_by_ten_squares_and_overflow_is_cut() -> None:
    meter = views.context_meter(latest(model="claude-haiku-4-5", usage=(0, 250_000, 0)), EMPTY, ITEMS)

    assert meter is not None and meter.columns == 10 and len(meter.squares) == 100
    assert "free" not in meter.squares and meter.left == 0


def test_the_latest_request_with_usage_and_the_compact_numbers() -> None:
    first, failed = request(10), request(None)
    assert views.latest_with_usage([first, failed]) is first
    assert views.latest_with_usage([failed]) is None
    assert [views.compact(n) for n in (950, 69_301, 1_000_000, None)] == ["950", "69.3k", "1.0M", "?"]
    assert views.percent(69_301, 1_000_000) == "6.9%" and views.percent(5, None) == ""


def test_each_request_gets_a_mark_for_what_liber_did() -> None:
    records = [request(10), request(None), request(20)]
    records[0].liber_outcome, records[2].liber_outcome = "passthrough", "rendered"

    chart = usage_chart(records, width=100)

    assert [(mark.x, mark.outcome) for mark in chart.marks] == [(0.0, "passthrough"), (100.0, "rendered")]
    assert usage_chart([request(5)]).marks[0].outcome == "unknown"  # recorded before Liber said
