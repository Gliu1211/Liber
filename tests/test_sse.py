"""Unit tests for liber/proxy/sse.py: SSE parsing, reply reassembly, and usage extraction."""

import itertools
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from liber.proxy.sse import ReplyCapture, SSEEvent, SSEParser, StreamAssembler, Usage
from tests.helpers import (
    NONSTREAM_FIXTURE,
    STREAM_ERROR_FIXTURE,
    STREAM_EXPECTED,
    STREAM_FIXTURE,
    load_json,
)


def parse_all(data: bytes) -> list[SSEEvent]:
    return SSEParser().feed(data)


def parse_in_pieces(data: bytes, cut_points: list[int]) -> list[SSEEvent]:
    parser = SSEParser()
    events: list[SSEEvent] = []
    edges = [0, *sorted(cut_points), len(data)]
    for start, end in itertools.pairwise(edges):
        events.extend(parser.feed(data[start:end]))
    return events


def assemble(events: list[SSEEvent]) -> StreamAssembler:
    assembler = StreamAssembler()
    for event in events:
        assembler.handle(event)
    return assembler


# --- SSEParser --------------------------------------------------------------------------------


def test_parser_reads_every_event_of_the_fixture() -> None:
    events = parse_all(STREAM_FIXTURE.read_bytes())

    names = [event.event for event in events]
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    assert names.count("content_block_start") == 3
    assert "ping" in names
    assert all(json.loads(event.data)["type"] == event.event for event in events)


@settings(max_examples=200, deadline=None)
@given(st.lists(st.integers(min_value=0, max_value=STREAM_FIXTURE.stat().st_size), max_size=40))
def test_parser_result_does_not_depend_on_chunk_boundaries(cut_points: list[int]) -> None:
    # The network may split the stream anywhere: inside a line, a JSON string, or a multi-byte
    # UTF-8 character. The parsed events must be identical however the bytes arrive.
    data = STREAM_FIXTURE.read_bytes()
    assert parse_in_pieces(data, cut_points) == parse_all(data)


def test_parser_accepts_crlf_line_endings() -> None:
    data = STREAM_FIXTURE.read_bytes()
    assert parse_all(data.replace(b"\n", b"\r\n")) == parse_all(data)


def test_parser_follows_the_sse_field_rules() -> None:
    data = (
        b": this is a comment and is ignored\n"
        b"data: line one\n"
        b"data: line two\n"
        b"\n"  # no `event:` field -> the SSE default name "message"
        b"event: no_data\n"
        b"\n"  # an event with no data lines is not dispatched
        b"event:tight\n"
        b"data:{}\n"
        b"\n"
        b"event: incomplete\n"
        b"data: still waiting for the blank line"
    )

    events = parse_all(data)

    assert events == [
        SSEEvent(event="message", data="line one\nline two"),
        SSEEvent(event="tight", data="{}"),
    ]


# --- StreamAssembler --------------------------------------------------------------------------


def test_assembler_rebuilds_the_final_message() -> None:
    assembler = assemble(parse_all(STREAM_FIXTURE.read_bytes()))

    assert assembler.message == load_json(STREAM_EXPECTED)
    assert assembler.complete
    assert assembler.error is None
    assert assembler.problem is None


def test_assembler_keeps_the_error_event() -> None:
    assembler = assemble(parse_all(STREAM_ERROR_FIXTURE.read_bytes()))

    assert assembler.error == {"type": "overloaded_error", "message": "Overloaded"}
    assert not assembler.complete
    assert assembler.message is not None
    assert assembler.message["content"] == [{"type": "text", "text": "Let me"}]


def test_assembler_ignores_unknown_event_and_delta_types() -> None:
    # The API may add new event and delta types (streaming docs, "Other events").
    events = parse_all(STREAM_FIXTURE.read_bytes())
    extras = [
        SSEEvent("brand_new_event", '{"type":"brand_new_event","x":1}'),
        SSEEvent(
            "content_block_delta",
            '{"type":"content_block_delta","index":0,"delta":{"type":"future_delta","x":1}}',
        ),
    ]
    assembler = assemble(events[:3] + extras + events[3:])

    assert assembler.message == load_json(STREAM_EXPECTED)


def test_usage_from_message_delta_is_cumulative_and_ignores_nulls() -> None:
    events = [
        SSEEvent(
            "message_start",
            json.dumps(
                {
                    "type": "message_start",
                    "message": {
                        "content": [],
                        "usage": {
                            "input_tokens": 10,
                            "cache_read_input_tokens": 100,
                            "output_tokens": 1,
                        },
                    },
                }
            ),
        ),
        SSEEvent(
            "message_delta",
            json.dumps(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {
                        "input_tokens": 12,
                        "cache_read_input_tokens": None,
                        "output_tokens": 40,
                    },
                }
            ),
        ),
    ]

    assembler = assemble(events)

    assert assembler.message is not None
    assert Usage.from_message(assembler.message) == Usage(
        input_tokens=12, output_tokens=40, cache_read_input_tokens=100
    )


def test_invalid_tool_input_json_is_reported_not_raised() -> None:
    events = [
        SSEEvent("message_start", '{"type":"message_start","message":{"content":[]}}'),
        SSEEvent(
            "content_block_start",
            '{"type":"content_block_start","index":0,'
            '"content_block":{"type":"tool_use","id":"t","name":"x","input":{}}}',
        ),
        SSEEvent(
            "content_block_delta",
            '{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"cut off"}}',
        ),
        SSEEvent("content_block_stop", '{"type":"content_block_stop","index":0}'),
    ]

    assembler = assemble(events)

    assert assembler.message is not None
    assert assembler.message["content"][0]["input"] == {}
    assert assembler.problem is not None and "tool input" in assembler.problem


# --- ReplyCapture -----------------------------------------------------------------------------


def test_capture_of_a_stream() -> None:
    capture = ReplyCapture("text/event-stream; charset=utf-8", content_encoding=None)
    for piece in [STREAM_FIXTURE.read_bytes()[i : i + 9] for i in range(0, 3000, 9)]:
        capture.feed(piece)

    reply = capture.result()

    assert reply.message == load_json(STREAM_EXPECTED)
    assert reply.usage == Usage(
        input_tokens=12,
        output_tokens=87,
        cache_read_input_tokens=20480,
        cache_creation_input_tokens=1500,
    )
    assert reply.stop_reason == "tool_use"
    assert reply.problem is None


def test_capture_of_a_json_message() -> None:
    capture = ReplyCapture("application/json", content_encoding=None)
    capture.feed(NONSTREAM_FIXTURE.read_bytes())

    reply = capture.result()

    assert reply.message == load_json(NONSTREAM_FIXTURE)
    assert reply.body_text == NONSTREAM_FIXTURE.read_text()
    assert reply.usage.cache_read_input_tokens == 2048
    assert reply.problem is None


def test_capture_of_a_json_error() -> None:
    capture = ReplyCapture("application/json", content_encoding=None)
    capture.feed(b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}')

    reply = capture.result()

    assert reply.message is None
    assert reply.problem is not None and "rate_limit_error" in reply.problem
    assert reply.usage == Usage()


def test_capture_never_raises_on_garbage() -> None:
    for content_type in ["text/event-stream", "application/json", "text/html", ""]:
        capture = ReplyCapture(content_type, content_encoding=None)
        capture.feed(b"\xff\xfe\x00 garbage {{{ \n\n data: [1,2\n\n")
        reply = capture.result()
        assert reply.message is None


def test_capture_skips_compressed_bodies() -> None:
    capture = ReplyCapture("application/json", content_encoding="gzip")
    capture.feed(b"\x1f\x8b\x08\x00 compressed bytes")

    reply = capture.result()

    assert reply.message is None
    assert reply.body_text is None
    assert reply.problem is not None and "gzip" in reply.problem


def test_capture_treats_media_types_case_insensitively() -> None:
    capture = ReplyCapture("Text/Event-Stream; charset=UTF-8", content_encoding=None)
    capture.feed(STREAM_FIXTURE.read_bytes())

    assert capture.result().usage.output_tokens == 87


def test_capture_reports_a_body_that_is_not_json() -> None:
    capture = ReplyCapture("text/html", content_encoding=None)
    capture.feed(b"<html>502 Bad Gateway</html>")

    reply = capture.result()

    assert reply.body_text == "<html>502 Bad Gateway</html>"
    assert reply.problem is not None and "not JSON" in reply.problem
