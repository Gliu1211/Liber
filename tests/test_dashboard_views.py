"""The dashboard's numbers (liber/dashboard/views.py), without a page around them."""

from datetime import UTC, datetime

from liber.dashboard import views
from liber.dashboard.views import usage_chart
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


def branch(branch_id: str, head: str) -> BranchRecord:
    now = datetime(2026, 11, 23, tzinfo=UTC)
    return BranchRecord(
        id=branch_id,
        session_id="s",
        parent_branch_id=None,
        fork_depth=0,
        head_hash=head,
        head_depth=1,
        created_at=now,
        last_seen_at=now,
    )


def node(hash_: str, parent: str, minute: int) -> views.NodeFacts:
    return views.NodeFacts(hash_, parent, datetime(2026, 11, 23, 22, minute, tzinfo=UTC))


def test_a_branch_whose_last_message_was_replaced_is_a_side_request() -> None:
    """Claude Code's recap: history + recap prompt, then the real next message in its place."""
    nodes = [
        node("m1", "root", 0),
        node("m2", "m1", 1),
        node("recap", "m2", 2),
        node("real", "m2", 3),
        node("m4", "real", 4),
    ]
    branches = [branch("first", "recap"), branch("main", "m4")]

    assert views.side_branches(branches, nodes) == {"first"}


def test_the_newest_of_two_last_messages_is_not_a_side_request() -> None:
    nodes = [node("m1", "root", 0), node("older", "m1", 1), node("newer", "m1", 2)]

    assert views.side_branches([branch("a", "older"), branch("b", "newer")], nodes) == {"a"}


def test_a_branch_that_diverged_further_back_stays_listed() -> None:
    """A rewind several messages back is a real alternative, not a side request."""
    nodes = [node("m1", "root", 0), node("x2", "m1", 1), node("x3", "x2", 2), node("y2", "m1", 3)]

    assert views.side_branches([branch("old", "x3"), branch("new", "y2")], nodes) == set()
