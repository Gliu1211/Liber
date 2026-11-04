"""Applying a plan when rendering (ARCHITECTURE.md §4 operations, D5/D8 frozen blocks, §5 boundaries).

render(parse(r), plan) turns the client's history into what Liber sends: summarized exchanges
become frozen blocks at the front of the first live user message, offloaded blocks become
pointer notes, deleted items disappear with their tool pair or exchange. Whatever the plan, the
result must pass the validator, render to the same bytes every time (ADR 0004 A2), and never
touch the client's own objects.
"""

import copy
import json
from typing import Any

import pytest

from liber.model.anthropic import ParsedRequest, parse, render
from liber.model.items import Kind, Operation, OperationKind, Plan
from liber.model.validate import validate
from liber.pipeline import encode_request
from liber.plan.apply import PlanNotApplicable, exchange_starts, summarize_operation
from tests.helpers import FIXTURES

LONG = FIXTURES / "requests" / "long_session_101.json"  # 10 exchanges of 4 tool rounds, thinking throughout
IN_FLIGHT = FIXTURES / "requests" / "tool_loop_in_flight.json"


def chat(*texts: str) -> dict[str, Any]:
    """A plain chat: user/assistant alternating, string content, starting with the user."""
    roles = ["user", "assistant"]
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 100,
        "system": "Be brief.",
        "messages": [{"role": roles[i % 2], "content": text} for i, text in enumerate(texts)],
    }


SIX_TURNS = chat("u0", "a0", "u1", "a1", "u2", "a2", "u3")  # exchanges start at 0, 2, 4, 6


def load(path: Any) -> dict[str, Any]:
    request: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return request


def ids_in(parsed: ParsedRequest, *, kind: Kind | None = None, message: int | None = None) -> list[str]:
    return [
        item.id
        for item in parsed.items
        if (kind is None or item.kind is kind) and (message is None or item.msg_index == message)
    ]


def rendered_with(request: dict[str, Any], *operations: Operation) -> dict[str, Any]:
    """Render `request` with these operations, checking what every plan must guarantee."""
    untouched = copy.deepcopy(request)
    parsed = parse(request)
    plan = Plan(operations=operations)

    result = render(parsed, plan)

    assert request == untouched  # the client's objects are never edited (ADR 0007's requirement)
    assert validate(result, original=untouched) == []
    # Same bytes on every render, also from a separately decoded copy (ADR 0004 A2).
    assert encode_request(render(parse(copy.deepcopy(untouched)), plan)) == encode_request(result)
    return result


# --- Summaries: frozen blocks (D5, D8) ----------------------------------------------------------


def test_a_summary_becomes_a_frozen_block_in_the_first_live_user_message() -> None:
    parsed = parse(SIX_TURNS)
    summary = summarize_operation(parsed.items, first=0, last=1, block_number=1)

    result = rendered_with(SIX_TURNS, summary)

    assert [m["role"] for m in result["messages"]] == ["user", "assistant", "user", "assistant", "user"]
    first_live = result["messages"][0]["content"]
    assert first_live[0] == {"type": "text", "text": summary.summary}
    assert first_live[1] == {"type": "text", "text": "u1"}  # the client's string became a text block
    assert summary.summary is not None and summary.summary.startswith('<liber:summary block="1" covers="msgs 0-1">')
    assert result["system"] == SIX_TURNS["system"] and result["model"] == SIX_TURNS["model"]


def test_appending_a_frozen_block_keeps_everything_before_it_byte_identical() -> None:
    # D5: a minor compaction appends; the system prompt, tools and earlier frozen blocks keep
    # their exact bytes, so the provider's prefix cache still hits up to there.
    parsed = parse(SIX_TURNS)
    first = summarize_operation(parsed.items, first=0, last=1, block_number=1)
    second = summarize_operation(parsed.items, first=2, last=3, block_number=2)

    before = encode_request(rendered_with(SIX_TURNS, first))
    after = encode_request(rendered_with(SIX_TURNS, first, second))

    frozen_one = encode_request({"type": "text", "text": first.summary})
    prefix = before[: before.index(frozen_one) + len(frozen_one)]
    assert after.startswith(prefix)
    assert json.loads(after)["messages"][0]["content"][1]["text"] == second.summary


def test_an_empty_message_at_the_start_belongs_to_the_summary() -> None:
    # An effort-only system message has no content blocks, so no item can name it; it still
    # sits inside the range being summarized.
    request = chat("u0", "a0", "u1", "a1", "u2")
    request["messages"].insert(0, {"role": "system", "content": []})
    parsed = parse(request)

    result = rendered_with(request, summarize_operation(parsed.items, first=0, last=2, block_number=1))

    assert [m["role"] for m in result["messages"]] == ["user", "assistant", "user"]
    assert 'covers="msgs 0-2"' in result["messages"][0]["content"][0]["text"]


def test_a_summary_must_start_where_the_live_region_starts() -> None:
    parsed = parse(SIX_TURNS)

    with pytest.raises(PlanNotApplicable, match="start"):
        render(parsed, Plan(operations=(summarize_operation(parsed.items, first=2, last=3, block_number=1),)))


def test_a_summary_must_end_just_before_a_new_exchange() -> None:
    # Ending at a user message would leave an assistant message first; ending at the latest
    # message would summarize the turn the model is about to answer.
    parsed = parse(SIX_TURNS)
    for last in (2, 6):
        with pytest.raises(PlanNotApplicable, match="exchange"):
            render(parsed, Plan(operations=(summarize_operation(parsed.items, first=0, last=last, block_number=1),)))


def test_a_summary_of_tool_loops_with_thinking_drops_that_thinking_from_the_front() -> None:
    request = load(LONG)
    parsed = parse(request)
    starts = exchange_starts(request["messages"])

    result = rendered_with(request, summarize_operation(parsed.items, first=0, last=starts[1] - 1, block_number=1))

    assert len(result["messages"]) == len(request["messages"]) - starts[1]
    assert result["messages"][0]["content"][1]["text"].startswith("Task 1:")


# --- Offload ----------------------------------------------------------------------------------


def test_offloading_a_tool_result_keeps_its_pairing() -> None:
    request = load(LONG)
    parsed = parse(request)
    [target] = [i for i in parsed.items if i.kind is Kind.TOOL_RESULT and i.msg_index == 2]
    offload = Operation(kind=OperationKind.OFFLOAD, item_ids=(target.id,), note="part_0.py")

    result = rendered_with(request, offload)

    block = result["messages"][2]["content"][0]
    assert block["type"] == "tool_result" and block["tool_use_id"] == target.tool_use_id
    assert block["content"] == (
        f'[Liber: offloaded item {target.content_hash[:12]} — "part_0.py", {target.tokens:,} tokens. '
        "Stored locally; restore from the Liber dashboard.]"
    )
    assert result["messages"][3:] == request["messages"][3:]


def test_offloading_text_keeps_its_cache_marker() -> None:
    # §5: the client's cache_control markers stay where their target still exists.
    request = copy.deepcopy(SIX_TURNS)
    marker = {"type": "ephemeral"}
    request["messages"][1]["content"] = [{"type": "text", "text": "a long answer", "cache_control": marker}]
    parsed = parse(request)

    result = rendered_with(request, Operation(kind=OperationKind.OFFLOAD, item_ids=tuple(ids_in(parsed, message=1))))

    [block] = result["messages"][1]["content"]
    assert block["text"].startswith("[Liber: offloaded item ") and block["cache_control"] == marker


def test_the_current_turn_is_never_edited() -> None:
    # The latest exchange is what the model is answering now.
    parsed = parse(SIX_TURNS)

    with pytest.raises(PlanNotApplicable, match="latest"):
        render(
            parsed, Plan(operations=(Operation(kind=OperationKind.OFFLOAD, item_ids=tuple(ids_in(parsed, message=6))),))
        )


@pytest.mark.parametrize("kind", [Kind.THINKING, Kind.TOOL_USE])
def test_thinking_and_tool_calls_cannot_be_offloaded(kind: Kind) -> None:
    parsed = parse(load(LONG))
    target = ids_in(parsed, kind=kind)[0]

    with pytest.raises(PlanNotApplicable):
        render(parsed, Plan(operations=(Operation(kind=OperationKind.OFFLOAD, item_ids=(target,)),)))


# --- Delete -----------------------------------------------------------------------------------


def test_deleting_a_tool_call_deletes_its_result_too() -> None:
    request = chat("u0")
    request["messages"] = [
        {"role": "user", "content": "check the files"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Looking."},
                {"type": "tool_use", "id": "t1", "name": "ls", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "cat", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a.py"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "print(1)"},
            ],
        },
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "thanks"},
    ]
    parsed = parse(request)
    [first_call] = [i.id for i in parsed.items if i.tool_use_id == "t1" and i.kind is Kind.TOOL_USE]

    result = rendered_with(request, Operation(kind=OperationKind.DELETE, item_ids=(first_call,)))

    assert [b.get("id") for b in result["messages"][1]["content"]] == [None, "t2"]
    assert [b["tool_use_id"] for b in result["messages"][2]["content"]] == ["t2"]


def test_deleting_everything_in_a_message_deletes_its_exchange() -> None:
    parsed = parse(SIX_TURNS)

    result = rendered_with(SIX_TURNS, Operation(kind=OperationKind.DELETE, item_ids=tuple(ids_in(parsed, message=3))))

    assert [m["content"] for m in result["messages"]] == ["u0", "a0", "u2", "a2", "u3"]


def test_the_latest_exchange_cannot_be_deleted() -> None:
    parsed = parse(SIX_TURNS)

    with pytest.raises(PlanNotApplicable, match="latest"):
        render(
            parsed, Plan(operations=(Operation(kind=OperationKind.DELETE, item_ids=tuple(ids_in(parsed, message=6))),))
        )


def test_thinking_is_never_deleted_on_its_own() -> None:
    parsed = parse(load(LONG))

    with pytest.raises(PlanNotApplicable, match="thinking"):
        render(
            parsed,
            Plan(operations=(Operation(kind=OperationKind.DELETE, item_ids=(ids_in(parsed, kind=Kind.THINKING)[0],)),)),
        )


# --- What a plan may not touch, and plans that don't fit ----------------------------------------


def test_the_tool_loop_in_progress_is_never_edited() -> None:
    request = load(IN_FLIGHT)  # ends with tool results: the model is mid-task
    parsed = parse(request)
    latest_result = [i for i in parsed.items if i.kind is Kind.TOOL_RESULT][-1]

    with pytest.raises(PlanNotApplicable, match="in progress"):
        render(parsed, Plan(operations=(Operation(kind=OperationKind.OFFLOAD, item_ids=(latest_result.id,)),)))


def test_a_plan_about_messages_this_request_does_not_have_does_not_apply() -> None:
    # e.g. the plan was made on another branch, or for later turns than this request has.
    later = parse(SIX_TURNS)
    earlier = parse(chat("u0", "a0", "u1"))

    with pytest.raises(PlanNotApplicable, match="doesn't have"):
        render(
            earlier, Plan(operations=(Operation(kind=OperationKind.OFFLOAD, item_ids=tuple(ids_in(later, message=4))),))
        )


def test_operations_inside_a_summarized_range_are_superseded() -> None:
    parsed = parse(SIX_TURNS)
    summary = summarize_operation(parsed.items, first=0, last=1, block_number=1)
    offload_in_range = Operation(kind=OperationKind.OFFLOAD, item_ids=tuple(ids_in(parsed, message=1)))

    with_offload = rendered_with(SIX_TURNS, offload_in_range, summary)

    assert with_offload == rendered_with(SIX_TURNS, summary)


# --- Review findings ----------------------------------------------------------------------------


def test_blocks_of_other_kinds_cannot_be_deleted_on_their_own() -> None:
    # A server tool call and its result are a pair Liber doesn't pair up; deleting one would
    # leave the other orphaned, which the validator doesn't check.
    request = chat("u0")
    request["messages"] = [
        {"role": "user", "content": "search the web"},
        {
            "role": "assistant",
            "content": [
                {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"query": "x"}},
                {"type": "web_search_tool_result", "tool_use_id": "srv_1", "content": []},
                {"type": "text", "text": "Found it."},
            ],
        },
        {"role": "user", "content": "thanks"},
    ]
    parsed = parse(request)
    server_call = ids_in(parsed, kind=Kind.OTHER)[0]

    with pytest.raises(PlanNotApplicable, match="can't be deleted"):
        render(parsed, Plan(operations=(Operation(kind=OperationKind.DELETE, item_ids=(server_call,)),)))


def test_an_operation_with_no_items_does_not_apply() -> None:
    parsed = parse(SIX_TURNS)

    for kind in OperationKind:
        with pytest.raises(PlanNotApplicable, match="no items"):
            render(parsed, Plan(operations=(Operation(kind=kind, item_ids=(), summary="x"),)))


def test_deleting_an_exchange_next_to_a_system_message_stays_valid() -> None:
    request = chat("u0", "a0", "u1", "a1", "u2")
    request["messages"].insert(3, {"role": "system", "content": "Be terse from now on."})  # after u1
    parsed = parse(request)

    result = rendered_with(request, Operation(kind=OperationKind.DELETE, item_ids=tuple(ids_in(parsed, message=1))))

    assert [m["content"] for m in result["messages"]] == ["u1", "Be terse from now on.", "a1", "u2"]
