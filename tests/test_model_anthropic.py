"""parse() / render() for the Messages API (ARCHITECTURE.md §4, Session 2).

The key property: with an empty plan, render(parse(r)) reproduces r exactly, for every
fixture and for generated requests. Session 4's edits are only safe if "no edit" is a no-op.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings

from liber.model.anthropic import UnsupportedPlan, parse, render
from liber.model.items import Kind, Operation, OperationKind, Plan, Role
from tests.helpers import FIXTURES
from tests.requests_strategy import requests

REQUEST_FIXTURES = sorted((FIXTURES / "requests").glob("*.json"))


def load(path: Path) -> dict[str, Any]:
    request: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return request


def round_trip(request: dict[str, Any]) -> dict[str, Any]:
    return render(parse(request), Plan())


# --- Round trip -------------------------------------------------------------------------------


@pytest.mark.parametrize("path", REQUEST_FIXTURES, ids=lambda p: p.name)
def test_empty_plan_reproduces_every_fixture_exactly(path: Path) -> None:
    request = load(path)

    rendered = round_trip(request)

    assert rendered == request
    # Same key order too, so a re-serialised request would be byte-identical.
    assert json.dumps(rendered) == json.dumps(request)


@settings(max_examples=300, deadline=None)
@given(requests)
def test_empty_plan_reproduces_generated_requests_exactly(request: dict[str, Any]) -> None:
    rendered = round_trip(request)

    assert rendered == request
    assert json.dumps(rendered) == json.dumps(request)


def test_parse_and_render_never_modify_the_input() -> None:
    request = load(FIXTURES / "requests" / "tool_loop_in_flight.json")
    untouched = copy.deepcopy(request)

    round_trip(request)

    assert request == untouched


# --- Items ------------------------------------------------------------------------------------


def test_every_content_block_becomes_an_item() -> None:
    parsed = parse(load(FIXTURES / "requests" / "tool_loop_in_flight.json"))

    summary = [(i.msg_index, i.block_index, i.role, i.kind) for i in parsed.items]
    assert summary == [
        (0, 0, Role.USER, Kind.TEXT),
        (1, 0, Role.ASSISTANT, Kind.THINKING),
        (1, 1, Role.ASSISTANT, Kind.TEXT),
        (1, 2, Role.ASSISTANT, Kind.TOOL_USE),
        (2, 0, Role.USER, Kind.TOOL_RESULT),
        (3, 0, Role.ASSISTANT, Kind.THINKING),
        (3, 1, Role.ASSISTANT, Kind.TOOL_USE),
        (3, 2, Role.ASSISTANT, Kind.TOOL_USE),
        (4, 0, Role.USER, Kind.TOOL_RESULT),
        (4, 1, Role.USER, Kind.TOOL_RESULT),
        (4, 2, Role.USER, Kind.TEXT),
    ]
    assert all(item.tokens > 0 and not item.tokens_exact for item in parsed.items)


def test_tool_results_depend_on_their_tool_use() -> None:
    parsed = parse(load(FIXTURES / "requests" / "tool_loop_in_flight.json"))
    by_position = {(i.msg_index, i.block_index): i for i in parsed.items}

    tool_use = by_position[(3, 2)]
    tool_result = by_position[(4, 1)]

    assert tool_use.tool_use_id == tool_result.tool_use_id == "toolu_fixture_03"
    assert tool_result.depends_on == tool_use.id
    assert tool_use.depends_on is None


def test_other_kinds_are_classified() -> None:
    parsed = parse(load(FIXTURES / "requests" / "redacted_thinking_and_media.json"))

    assert [i.kind for i in parsed.items] == [
        Kind.IMAGE,
        Kind.TEXT,
        Kind.THINKING,  # redacted_thinking is still thinking: never modified
        Kind.TEXT,
        Kind.DOCUMENT,
        Kind.TEXT,
    ]


def test_string_content_is_one_text_item_and_renders_back_as_a_string() -> None:
    request = load(FIXTURES / "requests" / "simple_text.json")

    parsed = parse(request)

    assert [(i.kind, i.block) for i in parsed.items][:1] == [(Kind.TEXT, {"type": "text", "text": "Hi"})]
    assert render(parsed, Plan())["messages"][0]["content"] == "Hi"


def test_mid_conversation_system_messages_are_items_with_system_role() -> None:
    parsed = parse(load(FIXTURES / "requests" / "mid_conversation_system.json"))

    assert parsed.items[-1].role == Role.SYSTEM


def test_item_ids_are_stable_and_depend_on_position() -> None:
    request = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "same words"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "same words"},
        ],
    }

    first, second = parse(request), parse(copy.deepcopy(request))

    assert [i.id for i in first.items] == [i.id for i in second.items]
    # Identical content at two positions: same content hash, different item ids.
    assert first.items[0].content_hash == first.items[2].content_hash
    assert first.items[0].id != first.items[2].id


def test_cache_control_markers_do_not_change_identity() -> None:
    # Claude Code moves its cache_control marker to the newest message every turn; that must
    # not make old items look new.
    marked = load(FIXTURES / "requests" / "tool_loop_in_flight.json")
    unmarked = copy.deepcopy(marked)
    for message in unmarked["messages"]:
        for block in message["content"]:
            block.pop("cache_control", None)
    for block in unmarked["system"]:
        block.pop("cache_control", None)
    for tool in unmarked["tools"]:
        tool.pop("cache_control", None)

    a, b = parse(marked), parse(unmarked)

    assert a.root_hash == b.root_hash
    assert a.chain_hashes == b.chain_hashes
    assert [i.id for i in a.items] == [i.id for i in b.items]
    assert [i.content_hash for i in a.items] == [i.content_hash for i in b.items]


def test_chain_hashes_cover_everything_before_a_message() -> None:
    request = load(FIXTURES / "requests" / "simple_text.json")
    changed_first = copy.deepcopy(request)
    changed_first["messages"][0]["content"] = "Hello"

    a, b = parse(request), parse(changed_first)

    # Changing message 0 changes the hash of every message after it, too.
    assert all(x != y for x, y in zip(a.chain_hashes, b.chain_hashes, strict=True))


def test_render_refuses_operations_until_they_are_implemented() -> None:
    parsed = parse(load(FIXTURES / "requests" / "simple_text.json"))
    plan = Plan(operations=(Operation(kind=OperationKind.DELETE, item_ids=(parsed.items[0].id,)),))

    with pytest.raises(UnsupportedPlan):
        render(parsed, plan)


@pytest.mark.parametrize(
    "bad_request",
    [
        {"model": "m"},  # no messages
        {"model": "m", "messages": "hello"},
        {"model": "m", "messages": [{"role": "user"}]},  # no content
        {"model": "m", "messages": [{"role": "user", "content": 42}]},
        {"model": "m", "messages": [{"role": "user", "content": ["not a block"]}]},
        ["not", "an", "object"],
    ],
)
def test_parse_rejects_requests_it_cannot_represent(bad_request: Any) -> None:
    # The pipeline catches this and forwards the original request unchanged.
    with pytest.raises(ValueError):
        parse(bad_request)
