"""parse() / render() for the Messages API (ARCHITECTURE.md §4, Session 2).

The key property: with an empty plan, render(parse(r)) reproduces r exactly, for every
fixture and for generated requests. Session 4's edits are only safe if "no edit" is a no-op.

parse(r, previous=...) reuses the work done for an earlier request of the same conversation
(ADR 0007). Its property: the result is exactly parse(r)'s, whatever `previous` is.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from liber.evals.replay import load_sessions, replay
from liber.model.anthropic import ParsedRequest, RecentRequests, UnsupportedPlan, parse, render
from liber.model.items import Kind, Operation, OperationKind, Plan, Role
from liber.session import chain
from tests.helpers import FIXTURES
from tests.requests_strategy import messages, requests

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


# --- Reusing the previous request's work (ADR 0007) -------------------------------------------


def computed(parsed: ParsedRequest) -> tuple[Any, ...]:
    """Everything parse() worked out, without the request objects it points at."""
    frames = [
        (f.index, f.role, f.content_is_string, f.chain_hash, f.block_hashes, f.block_tokens, f.has_numbers)
        for f in parsed.messages
    ]
    items = [
        (i.id, i.msg_index, i.block_index, i.role, i.kind, i.content_hash, i.tokens, i.tool_use_id, i.depends_on)
        for i in parsed.items
    ]
    return parsed.root_hash, frames, items


def assert_reuse_changes_nothing(request: dict[str, Any], previous: ParsedRequest) -> ParsedRequest:
    reused = parse(request, previous=previous)

    assert computed(reused) == computed(parse(copy.deepcopy(request)))
    # Whatever was reused, the frames and items point at *this* request's objects, which is what
    # render() rebuilds the request from.
    assert reused.original is request
    assert all(frame.message is request["messages"][frame.index] for frame in reused.messages)
    for item in reused.items:
        content = request["messages"][item.msg_index]["content"]
        if isinstance(content, list):
            assert item.block is content[item.block_index]
    rendered = render(reused, Plan())
    assert rendered == request and json.dumps(rendered) == json.dumps(request)
    return reused


@settings(max_examples=300, deadline=None)
@given(requests, requests)
def test_reuse_never_changes_the_result(earlier: dict[str, Any], request: dict[str, Any]) -> None:
    assert_reuse_changes_nothing(request, parse(earlier))


@settings(max_examples=300, deadline=None)
@given(requests, st.data())
def test_reuse_on_the_next_request_of_a_conversation(earlier: dict[str, Any], data: st.DataObject) -> None:
    # What a client sends next: the history up to some point (all of it, or less after a retry or
    # an edited prompt), maybe followed by new messages. Decoded again, as Liber receives it.
    keep = data.draw(st.integers(0, len(earlier["messages"])))
    later = json.loads(json.dumps(earlier))
    later["messages"] = later["messages"][:keep] + data.draw(st.lists(messages, max_size=3))

    assert_reuse_changes_nothing(later, parse(earlier))


def turns_of(session_name: str) -> list[dict[str, Any]]:
    [session] = [s for s in load_sessions(FIXTURES / "sessions") if s.name == session_name]
    return [json.loads(request.body) for request in replay(session)]


def test_only_messages_that_changed_or_are_new_are_hashed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The client's next turn: the same history, with its cache marker moved from the old last
    # message to the newest one, plus the assistant's reply and the new user message.
    *_, earlier, later = turns_of("long_agentic_101")
    previous = parse(earlier)
    hashed: list[dict[str, Any]] = []
    real_canonical_block = chain.canonical_block
    monkeypatch.setattr(chain, "canonical_block", lambda block: hashed.append(block) or real_canonical_block(block))

    reused = parse(later, previous=previous)
    monkeypatch.undo()  # stop counting: the check below parses from scratch, for comparison
    assert_reuse_changes_nothing(later, previous)

    old_last = len(earlier["messages"]) - 1  # lost its cache marker, so it is compared, unequal, and redone
    assert len(later["messages"]) == old_last + 3
    assert hashed == [block for message in later["messages"][old_last:] for block in message["content"]]
    assert reused.messages[old_last].chain_hash == previous.messages[old_last].chain_hash  # markers don't count


def test_reuse_tells_numbers_and_booleans_apart() -> None:
    # Python's == says 1 == 1.0 == True, but they are different JSON (and different tokens for
    # the model), so a message that changed only that way must get a new hash.
    def request_with(limit: Any) -> dict[str, Any]:
        return {
            "model": "m",
            "messages": [
                {"role": "user", "content": "read it"},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "name": "read", "input": {"limit": limit}}],
                },
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
            ],
        }

    previous = parse(request_with(1))
    for other in (True, 1.0):
        reused = assert_reuse_changes_nothing(request_with(other), previous)
        assert reused.messages[1].chain_hash != previous.messages[1].chain_hash
    # And unchanged numbers are still recognised as unchanged.
    assert parse(request_with(1), previous=previous).chain_hashes == previous.chain_hashes


def test_a_different_system_prompt_reuses_nothing() -> None:
    earlier = load(FIXTURES / "requests" / "simple_text.json")
    later = copy.deepcopy(earlier)
    later["system"] = "You are a pirate."

    reused = assert_reuse_changes_nothing(later, parse(earlier))

    assert reused.chain_hashes[0] != parse(earlier).chain_hashes[0]


# --- RecentRequests: which earlier request to reuse ---------------------------------------------


def conversation(first: str, *more: str, system: str = "Be brief.") -> dict[str, Any]:
    roles = ["user", "assistant"]
    texts = [first, *more]
    return {
        "model": "m",
        "system": system,
        "messages": [{"role": roles[i % 2], "content": t} for i, t in enumerate(texts)],
    }


def test_recent_requests_finds_the_latest_request_of_the_same_conversation() -> None:
    recent = RecentRequests()
    first_turn = parse(conversation("hi"))
    recent.remember(first_turn)

    assert recent.find(conversation("hi", "hello", "how are you?")) is first_turn
    assert recent.find(conversation("something else")) is None  # another conversation
    assert recent.find(conversation("hi", system="Be verbose.")) is None  # same words, other system prompt
    assert recent.find({"model": "m", "messages": []}) is None


def test_recent_requests_keeps_one_entry_per_conversation_and_drops_the_oldest() -> None:
    recent = RecentRequests(size=2)
    a1 = parse(conversation("a"))
    recent.remember(a1)
    a2 = parse(conversation("a", "ok", "more"), previous=recent.find(conversation("a", "ok", "more")))
    recent.remember(a2, replacing=a1)
    assert recent.entries == [a2]  # the conversation's newer request replaced its older one

    b1, c1 = parse(conversation("b")), parse(conversation("c"))
    recent.remember(b1)
    recent.remember(c1)
    assert recent.entries == [c1, b1]  # a2 was used least recently


def test_requests_without_messages_are_not_remembered() -> None:
    recent = RecentRequests()

    recent.remember(parse({"model": "m", "messages": []}))

    assert recent.entries == []
