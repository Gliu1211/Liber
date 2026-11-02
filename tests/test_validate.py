"""The validity rules of ARCHITECTURE.md §5 (liber/model/validate.py).

Each test starts from a valid fixture, breaks one thing, and checks the validator names it.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from liber.model.validate import Violation, in_flight_tool_loop_start, validate
from tests.helpers import FIXTURES

REQUESTS = FIXTURES / "requests"


def load(name: str) -> dict[str, Any]:
    request: dict[str, Any] = json.loads((REQUESTS / name).read_text(encoding="utf-8"))
    return request


def rules(violations: list[Violation]) -> set[str]:
    return {v.rule for v in violations}


@pytest.mark.parametrize("path", sorted(REQUESTS.glob("*.json")), ids=lambda p: p.name)
def test_every_fixture_is_valid(path: Path) -> None:
    request = json.loads(path.read_text(encoding="utf-8"))
    assert validate(request, original=request) == []


# --- Structure --------------------------------------------------------------------------------


def test_missing_or_empty_messages() -> None:
    assert rules(validate({"model": "m"})) == {"messages_present"}
    assert rules(validate({"model": "m", "messages": []})) == {"messages_present"}


def test_first_message_must_be_from_the_user() -> None:
    request = load("simple_text.json")
    request["messages"] = request["messages"][1:]  # now starts with the assistant

    assert "first_is_user" in rules(validate(request))


def test_roles_must_alternate() -> None:
    request = load("simple_text.json")
    request["messages"].insert(1, {"role": "user", "content": "and another thing"})

    violations = validate(request)

    assert rules(violations) == {"alternation"}
    assert "messages[1]" in str(violations[0])


def test_unknown_roles_are_rejected() -> None:
    request = load("simple_text.json")
    request["messages"][1]["role"] = "tool"

    assert "message_shape" in rules(validate(request))


@pytest.mark.parametrize(
    "content",
    ["", [], [{"type": "text", "text": ""}]],
    ids=["empty string", "empty list", "empty text block"],
)
def test_empty_content_is_rejected(content: Any) -> None:
    request = load("simple_text.json")
    request["messages"][1]["content"] = content

    assert "empty_content" in rules(validate(request))


# --- Tool pairing -----------------------------------------------------------------------------


def test_orphan_tool_use_is_caught() -> None:
    request = load("tool_loop_in_flight.json")
    # Drop the result for toolu_fixture_03, keep the one for toolu_fixture_02.
    del request["messages"][4]["content"][1]

    violations = validate(request)

    assert rules(violations) == {"tool_pairing"}
    assert "toolu_fixture_03" in str(violations[0])


def test_orphan_tool_result_is_caught() -> None:
    request = load("tool_loop_in_flight.json")
    request["messages"][2]["content"][0]["tool_use_id"] = "toolu_does_not_exist"

    found = rules(validate(request))

    # The result now points nowhere, and toolu_fixture_01 lost its result.
    assert found == {"tool_pairing"}
    assert len([v for v in validate(request) if v.rule == "tool_pairing"]) == 2


def test_tool_result_must_be_in_the_very_next_message() -> None:
    request = load("tool_loop_in_flight.json")
    # Move the first exchange's result two messages later: pairing is positional, not global.
    result = request["messages"][2]["content"].pop(0)
    request["messages"][4]["content"].insert(0, result)
    request["messages"][2]["content"] = [{"type": "text", "text": "(nothing)"}]

    assert "tool_pairing" in rules(validate(request))


def test_tool_results_must_come_before_other_blocks() -> None:
    request = load("tool_loop_in_flight.json")
    last = request["messages"][4]["content"]
    last.insert(0, last.pop())  # the text block now precedes the tool results

    assert "tool_results_first" in rules(validate(request))


# --- Relative to the original request ----------------------------------------------------------


@pytest.mark.parametrize("field", ["thinking", "signature"])
def test_edited_thinking_block_is_caught(field: str) -> None:
    original = load("long_session_101.json")
    rendered = copy.deepcopy(original)
    rendered["messages"][1]["content"][0][field] += " (edited)"

    violations = validate(rendered, original=original)

    assert rules(violations) == {"thinking_unchanged"}
    assert "messages[1]" in str(violations[0])


def test_edited_redacted_thinking_block_is_caught() -> None:
    original = load("redacted_thinking_and_media.json")
    rendered = copy.deepcopy(original)
    rendered["messages"][1]["content"][0]["data"] = "tampered"

    assert rules(validate(rendered, original=original)) == {"thinking_unchanged"}


def test_in_flight_tool_loop_must_be_untouched() -> None:
    original = load("tool_loop_in_flight.json")
    rendered = copy.deepcopy(original)
    rendered["messages"][2]["content"][0]["content"] = "[Liber: offloaded]"

    assert rules(validate(rendered, original=original)) == {"in_flight_loop"}


def test_in_flight_tool_loop_detection() -> None:
    # Mid-loop: the last message is a tool result, and the loop began after message 0.
    assert in_flight_tool_loop_start(load("tool_loop_in_flight.json")["messages"]) == 1
    # A plain user turn starts a new exchange: no loop is in flight.
    assert in_flight_tool_loop_start(load("simple_text.json")["messages"]) is None
    assert in_flight_tool_loop_start(load("long_session_101.json")["messages"]) is None


# --- Cache breakpoints ------------------------------------------------------------------------


def test_at_most_four_cache_breakpoints() -> None:
    request = load("tool_loop_in_flight.json")  # already has 3 markers
    assert validate(request) == []

    request["messages"][0]["content"][0]["cache_control"] = {"type": "ephemeral"}
    assert validate(request) == []  # 4 is the limit

    request["messages"][1]["content"][1]["cache_control"] = {"type": "ephemeral"}
    assert rules(validate(request)) == {"cache_breakpoints"}


# --- Mid-conversation system messages --------------------------------------------------------


def test_system_message_cannot_come_first() -> None:
    request = load("simple_text.json")
    request["messages"].insert(0, {"role": "system", "content": "Be brief."})

    assert "system_placement" in rules(validate(request))


def test_system_message_must_be_last_or_followed_by_the_assistant() -> None:
    request = load("mid_conversation_system.json")
    request["messages"].append({"role": "user", "content": "one more question"})

    assert "system_placement" in rules(validate(request))


def test_effort_only_system_message_may_sit_anywhere() -> None:
    request = load("simple_text.json")
    request["messages"].insert(0, {"role": "system", "content": [], "output_config": {"effort": "low"}})

    assert validate(request) == []


def test_violations_are_readable() -> None:
    request = load("simple_text.json")
    request["messages"].insert(1, {"role": "user", "content": "again"})

    [violation] = validate(request)

    assert str(violation).startswith("alternation: messages[1]")
