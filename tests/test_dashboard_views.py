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
