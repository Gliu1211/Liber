"""Validity rules for a rendered Messages API request (ARCHITECTURE.md §5).

Why it exists: Liber rewrites requests (from Session 4), and a request the API rejects would
break the user's session. So every rendered request is checked here first; if anything is
wrong, the pipeline forwards the client's original request instead (hard rule 1).

`validate(request, original)` returns a list of readable violations (empty = valid):

    messages_present   there is at least one message
    message_shape      each message has a known role and string-or-blocks content
    first_is_user      the first user/assistant message is from the user
    alternation        user and assistant messages alternate (system messages aside)
    system_placement   a mid-conversation system message is not first, follows a user turn,
                       and is last or followed by an assistant turn (effort-only ones are exempt)
    empty_content      no empty messages, no empty or whitespace-only text blocks
    tool_pairing       each tool_use has its tool_result in the very next message, and vice versa;
                       no repeated ids
    tool_results_first in a user message, tool_result blocks come before anything else
    cache_breakpoints  at most 4 cache_control markers
  relative to the client's original request:
    thinking_unchanged thinking blocks are exactly the client's, in place; only the oldest may go
    in_flight_loop     the tool loop in progress is exactly the client's

Rules were checked against the current API docs (2026-11). The API rejects non-alternating
roles ("roles must alternate"), so the stricter reading is used. A false alarm only means the
original request is forwarded unchanged, which is always safe.

Preserved thinking (newer models): a thinking block is also bound to everything *before* it,
so editing earlier history can invalidate later thinking blocks even if the blocks themselves
are untouched. That matters once plans edit history (Session 4); see
docs/decisions/0004-preserved-thinking-and-history-edits.md.
"""

from dataclasses import dataclass
from itertools import pairwise
from typing import Any

ROLES = ("user", "assistant", "system")
THINKING_TYPES = ("thinking", "redacted_thinking")
MAX_CACHE_BREAKPOINTS = 4  # per request, per the prompt caching docs


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.message}"


def validate(request: Any, original: Any = None) -> list[Violation]:
    """Check `request` against §5. With `original`, also check what Liber must not change."""
    messages = request.get("messages") if isinstance(request, dict) else None
    if not isinstance(messages, list) or not messages:
        return [Violation("messages_present", "the request has no messages")]

    shape_problems = check_shape(messages)
    if shape_problems:
        return shape_problems  # the other rules assume well-formed messages

    violations = [
        *check_order(messages),
        *check_system_placement(messages),
        *check_empty_content(messages),
        *check_tool_pairing(messages),
        *check_tool_results_first(messages),
        *check_cache_breakpoints(request),
    ]
    original_messages = original.get("messages") if isinstance(original, dict) else None
    if isinstance(original_messages, list):
        violations += check_thinking_unchanged(messages, original_messages)
        violations += check_in_flight_loop(messages, original_messages)
    return violations


# --- Helpers ----------------------------------------------------------------------------------


def blocks_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message["content"]
    return content if isinstance(content, list) else []


def tool_use_ids(message: dict[str, Any] | None) -> list[str]:
    if message is None or message["role"] != "assistant":
        return []
    return [block.get("id", "") for block in blocks_of(message) if block.get("type") == "tool_use"]


def tool_result_ids(message: dict[str, Any] | None) -> list[str]:
    if message is None or message["role"] != "user":
        return []
    return [block.get("tool_use_id", "") for block in blocks_of(message) if block.get("type") == "tool_result"]


def is_effort_only(message: dict[str, Any]) -> bool:
    """A system message with empty content only changes settings (e.g. output_config.effort)."""
    return message["role"] == "system" and not message["content"]


def in_flight_tool_loop_start(messages: list[Any]) -> int | None:
    """Index of the first message of the tool loop in progress, or None if none is in progress.

    A loop is in progress when the last message carries tool results: the model is mid-turn,
    calling tools. The loop began right after the last user message that *started* an exchange
    (one with no tool results). Plans must never touch these messages (§5).
    """
    if not messages or not tool_results_in(messages[-1]):
        return None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user" and not tool_results_in(message):
            return index + 1
    return 0


def tool_results_in(message: Any) -> bool:
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


# --- Rules ------------------------------------------------------------------------------------


def check_shape(messages: list[Any]) -> list[Violation]:
    violations = []
    for i, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        well_formed = (
            isinstance(message, dict)
            and message.get("role") in ROLES
            and (isinstance(content, str) or (isinstance(content, list) and all(isinstance(b, dict) for b in content)))
        )
        if not well_formed:
            violations.append(
                Violation("message_shape", f"messages[{i}] needs a role in {ROLES} and string or block-list content")
            )
    return violations


def check_order(messages: list[dict[str, Any]]) -> list[Violation]:
    # Mid-conversation system messages sit between turns; alternation is about the turns.
    turns = [(i, m["role"]) for i, m in enumerate(messages) if m["role"] != "system"]
    if not turns:
        return [Violation("first_is_user", "there is no user message")]
    violations = []
    first_index, first_role = turns[0]
    if first_role != "user":
        violations.append(Violation("first_is_user", f"messages[{first_index}] is from the {first_role}"))
    for (_, previous_role), (i, role) in pairwise(turns):
        if role == previous_role:
            violations.append(Violation("alternation", f"messages[{i}] is a second {role} turn in a row"))
    return violations


def check_system_placement(messages: list[dict[str, Any]]) -> list[Violation]:
    violations = []
    for i, message in enumerate(messages):
        if message["role"] != "system" or is_effort_only(message):
            continue
        if i == 0:
            violations.append(Violation("system_placement", "messages[0] is a system message; it can't come first"))
            continue
        previous = messages[i - 1]
        follows_server_tool = previous["role"] == "assistant" and any(
            block.get("type") == "server_tool_use" for block in blocks_of(previous)[-1:]
        )
        if previous["role"] != "user" and not follows_server_tool:
            violations.append(Violation("system_placement", f"messages[{i}] is a system message after a non-user turn"))
        if i + 1 < len(messages) and messages[i + 1]["role"] != "assistant":
            violations.append(
                Violation("system_placement", f"messages[{i}] is a system message not followed by an assistant turn")
            )
    return violations


def check_empty_content(messages: list[dict[str, Any]]) -> list[Violation]:
    violations = []
    for i, message in enumerate(messages):
        if is_effort_only(message):
            continue
        if not message["content"]:
            violations.append(Violation("empty_content", f"messages[{i}] has empty content"))
            continue
        for j, block in enumerate(blocks_of(message)):
            if block.get("type") == "text" and not str(block.get("text", "")).strip():
                violations.append(
                    Violation(
                        "empty_content", f"messages[{i}].content[{j}] is an empty (or whitespace-only) text block"
                    )
                )
    return violations


def check_tool_pairing(messages: list[dict[str, Any]]) -> list[Violation]:
    """Pairing is positional: results answer the calls of the message right before them."""
    violations = []
    for i, message in enumerate(messages):
        calls, results = tool_use_ids(message), tool_result_ids(message)
        for ids, what in ((calls, "tool_use"), (results, "tool_result")):
            if len(ids) != len(set(ids)):
                violations.append(Violation("tool_pairing", f"messages[{i}] repeats a {what} id"))

        following = messages[i + 1] if i + 1 < len(messages) else None
        answered = set(tool_result_ids(following))
        for tool_id in calls:
            if tool_id not in answered:
                violations.append(
                    Violation(
                        "tool_pairing", f"messages[{i}]: tool_use {tool_id} has no tool_result in messages[{i + 1}]"
                    )
                )
        preceding = messages[i - 1] if i > 0 else None
        called = set(tool_use_ids(preceding))
        for tool_id in results:
            if tool_id not in called:
                violations.append(
                    Violation(
                        "tool_pairing", f"messages[{i}]: tool_result for {tool_id} has no tool_use in messages[{i - 1}]"
                    )
                )
    return violations


def check_tool_results_first(messages: list[dict[str, Any]]) -> list[Violation]:
    violations = []
    for i, message in enumerate(messages):
        if message["role"] != "user":
            continue
        seen_other_block = False
        for j, block in enumerate(blocks_of(message)):
            if block.get("type") != "tool_result":
                seen_other_block = True
            elif seen_other_block:
                violations.append(
                    Violation("tool_results_first", f"messages[{i}].content[{j}] is a tool_result after other content")
                )
                break
    return violations


def check_cache_breakpoints(request: dict[str, Any]) -> list[Violation]:
    markers = 1 if "cache_control" in request else 0  # top-level automatic caching uses one
    places: list[Any] = []
    for key in ("tools", "system"):
        if isinstance(request.get(key), list):
            places += request[key]
    for message in request["messages"]:
        for block in blocks_of(message):
            places.append(block)
            if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                places += block["content"]
    markers += sum(1 for place in places if isinstance(place, dict) and "cache_control" in place)
    if markers > MAX_CACHE_BREAKPOINTS:
        return [
            Violation("cache_breakpoints", f"{markers} cache_control markers; the limit is {MAX_CACHE_BREAKPOINTS}")
        ]
    return []


def thinking_groups(messages: list[Any]) -> list[tuple[int, list[dict[str, Any]]]]:
    """(message index, its thinking/redacted_thinking blocks) for each message that has any."""
    groups = []
    for index, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            group = [b for b in content if isinstance(b, dict) and b.get("type") in THINKING_TYPES]
            if group:
                groups.append((index, group))
    return groups


def check_thinking_unchanged(messages: list[dict[str, Any]], original_messages: list[Any]) -> list[Violation]:
    """Thinking blocks must be the client's own, unedited, in the same messages and order.

    Preserved thinking (docs/decisions/0004) binds each block to what came before it, so the
    only change allowed is dropping whole messages' worth of thinking from the *front*: the
    rendered thinking must be the tail end of the client's.
    """
    rendered = thinking_groups(messages)
    client = [group for _, group in thinking_groups(original_messages)]
    if len(rendered) > len(client):
        return [Violation("thinking_unchanged", "the request has thinking blocks the client never sent")]
    expected = client[len(client) - len(rendered) :]  # the tail of the client's thinking
    for (index, group), client_group in zip(rendered, expected, strict=True):
        # == on dicts compares every field exactly, signature included.
        if group != client_group:
            return [
                Violation(
                    "thinking_unchanged",
                    f"messages[{index}]: thinking differs from the client's (edited, moved, or dropped out of order)",
                )
            ]
    return []


def check_in_flight_loop(messages: list[dict[str, Any]], original_messages: list[Any]) -> list[Violation]:
    start = in_flight_tool_loop_start(original_messages)
    if start is None:
        return []
    loop = original_messages[start:]
    if messages[len(messages) - len(loop) :] != loop:
        return [Violation("in_flight_loop", f"the tool loop in progress (the client's messages[{start}:]) was changed")]
    return []
