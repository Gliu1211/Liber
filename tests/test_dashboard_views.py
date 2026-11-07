"""The dashboard's numbers (liber/dashboard/views.py), without a page around them."""

from datetime import UTC, datetime

from liber.dashboard.views import usage_chart
from liber.session.store import RequestRecord


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
