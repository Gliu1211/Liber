"""Session identification with the message hash chain (ARCHITECTURE.md D7, Session 2).

Conversations are a tree of messages, like git commits. A request is matched against the
longest stored prefix; what follows is new. These tests walk through the cases D7 names.
"""

import asyncio
import copy
import sqlite3
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from liber.model.anthropic import parse
from liber.session.chain import Identity, StoreBusy, canonical_json, content_hash, identify
from liber.session.store import Store

SYSTEM = "You are a helpful assistant."


def request(*texts: str, system: str = SYSTEM) -> dict[str, Any]:
    """A request whose messages alternate user/assistant, starting with the user."""
    roles = ["user", "assistant"]
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 100,
        "system": system,
        "messages": [{"role": roles[i % 2], "content": text} for i, text in enumerate(texts)],
    }


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    store = Store(tmp_path / "liber.db")
    await store.create_tables()
    yield store
    await store.close()


async def see(store: Store, body: dict[str, Any]) -> Identity:
    return await identify(store, parse(body))


# --- Canonicalization -------------------------------------------------------------------------


def test_canonical_json_is_stable() -> None:
    assert canonical_json({"b": 1, "a": [2, {"d": 3, "c": 4}]}) == '{"a":[2,{"c":4,"d":3}],"b":1}'
    assert canonical_json({"text": "café"}) == '{"text":"café"}'


def test_content_hash_ignores_cache_control_and_key_order() -> None:
    plain = {"type": "text", "text": "hello"}
    marked = {"text": "hello", "type": "text", "cache_control": {"type": "ephemeral"}}
    nested = {"type": "tool_result", "tool_use_id": "t", "content": [plain]}
    nested_marked = {"type": "tool_result", "tool_use_id": "t", "content": [marked], "cache_control": {"type": "x"}}

    assert content_hash(plain) == content_hash(marked)
    assert content_hash(nested) == content_hash(nested_marked)
    assert content_hash(plain) != content_hash({"type": "text", "text": "hello!"})


def test_string_and_block_content_are_the_same_conversation() -> None:
    # The API treats "hi" and [{"type": "text", "text": "hi"}] as the same message.
    as_blocks = request("hi")
    as_blocks["messages"][0]["content"] = [{"type": "text", "text": "hi"}]

    assert parse(as_blocks).chain_hashes == parse(request("hi")).chain_hashes


# --- Identification ---------------------------------------------------------------------------


async def test_first_request_starts_a_new_session(store: Store) -> None:
    identity = await see(store, request("u1"))

    assert identity.new_session and identity.new_branch
    assert (identity.known_messages, identity.new_messages) == (0, 1)


async def test_a_growing_conversation_stays_on_one_branch(store: Store) -> None:
    first = await see(store, request("u1"))
    second = await see(store, request("u1", "a1", "u2"))
    third = await see(store, request("u1", "a1", "u2", "a2", "u3"))

    assert first.session_id == second.session_id == third.session_id
    assert first.branch_id == second.branch_id == third.branch_id
    assert not second.new_branch and not third.new_branch
    assert (second.known_messages, second.new_messages) == (1, 2)
    assert (third.known_messages, third.new_messages) == (3, 2)


async def test_a_repeated_request_has_nothing_new(store: Store) -> None:
    first = await see(store, request("u1", "a1", "u2"))
    retry = await see(store, request("u1", "a1", "u2"))

    assert retry.branch_id == first.branch_id
    assert (retry.known_messages, retry.new_messages) == (3, 0)


async def test_two_conversations_sharing_an_opening_are_distinguished(store: Store) -> None:
    a = await see(store, request("hi", "hello!", "help me with Python"))
    b = await see(store, request("hi", "hello!", "help me with Rust"))

    assert b.branch_id != a.branch_id
    assert (b.known_messages, b.new_messages) == (2, 1)  # the shared opening is not new

    # Each conversation keeps continuing on its own branch.
    a_next = await see(store, request("hi", "hello!", "help me with Python", "sure", "thanks"))
    b_next = await see(store, request("hi", "hello!", "help me with Rust", "sure", "thanks"))
    assert a_next.branch_id == a.branch_id
    assert b_next.branch_id == b.branch_id


async def test_a_divergence_creates_a_branch(store: Store) -> None:
    main = await see(store, request("u1", "a1", "u2", "a2", "u3"))
    # The user went back and asked something else at message 3 (e.g. an edited prompt).
    fork = await see(store, request("u1", "a1", "u2", "a2", "u3 edited"))

    assert fork.new_branch and not fork.new_session
    assert fork.session_id == main.session_id
    assert fork.parent_branch_id == main.branch_id
    assert fork.fork_depth == 4
    assert (fork.known_messages, fork.new_messages) == (4, 1)


async def test_rewriting_early_history_starts_a_new_branch(store: Store) -> None:
    original = await see(store, request("u1", "a1", "u2", "a2", "u3"))
    # A client that trims or rewrites an old message (here message 1) changes every hash after
    # it: only the messages before the edit still match.
    rewritten = await see(store, request("u1", "a1 (trimmed)", "u2", "a2", "u3"))

    assert rewritten.new_branch
    assert rewritten.session_id == original.session_id
    assert (rewritten.known_messages, rewritten.new_messages) == (1, 4)


async def test_rewriting_the_whole_history_starts_a_new_session(store: Store) -> None:
    # e.g. the client's own auto-compaction replaced everything with a summary.
    original = await see(store, request("u1", "a1", "u2", "a2", "u3"))
    compacted = await see(store, request("Summary of our conversation so far: ...", "ok", "u4"))

    assert compacted.new_session
    assert compacted.session_id != original.session_id


async def test_a_different_system_prompt_is_a_different_conversation(store: Store) -> None:
    a = await see(store, request("hi"))
    b = await see(store, request("hi", system="You are a pirate."))

    assert b.session_id != a.session_id


async def test_moving_cache_control_markers_does_not_branch(store: Store) -> None:
    first = request("u1", "a1", "u2")
    first["messages"][2]["content"] = [{"type": "text", "text": "u2", "cache_control": {"type": "ephemeral"}}]
    second = request("u1", "a1", "u2", "a2", "u3")
    second["messages"][4]["content"] = [{"type": "text", "text": "u3", "cache_control": {"type": "ephemeral"}}]

    a = await see(store, first)
    b = await see(store, second)

    assert b.branch_id == a.branch_id
    assert (b.known_messages, b.new_messages) == (3, 2)


async def test_requests_without_messages_are_not_registered(store: Store) -> None:
    identity = await see(store, {"model": "m", "messages": []})

    assert identity.session_id is None
    assert await store.list_sessions() == []


# --- Registration -----------------------------------------------------------------------------


async def test_new_items_are_registered_once(store: Store) -> None:
    first = await see(store, request("u1", "a1", "u2"))
    await see(store, request("u1", "a1", "u2", "a2", "u3"))
    await see(store, request("u1", "a1", "u2", "a2", "u3"))  # a retry adds nothing

    items = await store.list_items(first.session_id)

    assert [(item.msg_index, item.role) for item in items] == [
        (0, "user"),
        (1, "assistant"),
        (2, "user"),
        (3, "assistant"),
        (4, "user"),
    ]
    assert all(item.tokens > 0 and not item.tokens_exact for item in items)


async def test_branches_share_the_items_of_their_common_prefix(store: Store) -> None:
    a = await see(store, request("u1", "a1", "u2"))
    b = await see(store, request("u1", "a1", "u2 other"))

    items = await store.list_items(a.session_id)

    assert a.session_id == b.session_id
    # u1 and a1 are stored once; each branch adds its own third message.
    assert len(items) == 4


async def test_registered_items_match_the_parsed_items(store: Store) -> None:
    body = request("u1", "a1", "u2")
    body["messages"][1]["content"] = [
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "ls"}},
    ]
    body["messages"][2]["content"] = [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "README.md"}]
    parsed = parse(copy.deepcopy(body))

    identity = await identify(store, parsed)
    stored = await store.list_items(identity.session_id)

    assert [s.id for s in stored] == [i.id for i in parsed.items]
    assert [s.kind for s in stored] == ["text", "text", "tool_use", "tool_result"]
    assert stored[3].depends_on == stored[2].id
    assert stored[3].tool_use_id == "toolu_1"


# --- Review findings: more tree shapes, concurrency, locking ------------------------------------


async def test_a_fork_of_a_fork(store: Store) -> None:
    main = await see(store, request("u1", "a1", "u2", "a2", "u3"))
    fork = await see(store, request("u1", "a1", "u2 alt", "a2", "u3"))
    fork_of_fork = await see(store, request("u1", "a1", "u2 alt", "a2", "u3 alt"))

    assert fork.parent_branch_id == main.branch_id
    assert fork_of_fork.parent_branch_id == fork.branch_id
    assert fork_of_fork.fork_depth == 4
    assert len({main.branch_id, fork.branch_id, fork_of_fork.branch_id}) == 3


async def test_rewinding_to_an_earlier_point_then_diverging(store: Store) -> None:
    main = await see(store, request("u1", "a1", "u2", "a2", "u3"))
    rewind = await see(store, request("u1", "a1", "u2"))  # e.g. a retry from an earlier turn
    diverge = await see(store, request("u1", "a1", "u2", "a2 different", "u3"))

    assert (rewind.branch_id, rewind.known_messages, rewind.new_messages) == (main.branch_id, 3, 0)
    assert diverge.new_branch and diverge.parent_branch_id == main.branch_id
    assert diverge.fork_depth == 3


async def test_a_retry_after_a_fork_stays_on_the_fork(store: Store) -> None:
    await see(store, request("u1", "a1", "u2"))
    fork = await see(store, request("u1", "a1", "u2 alt"))
    retry = await see(store, request("u1", "a1", "u2 alt"))

    assert (retry.branch_id, retry.new_branch, retry.new_messages) == (fork.branch_id, False, 0)


async def test_concurrent_identical_requests_make_one_conversation(store: Store) -> None:
    body = request("u1", "a1", "u2")

    first, second = await asyncio.gather(see(store, body), see(store, copy.deepcopy(body)))

    assert first.session_id == second.session_id and first.branch_id == second.branch_id
    assert len(await store.list_sessions()) == 1
    assert len(await store.list_items(first.session_id)) == 3


async def test_concurrent_different_continuations_branch_cleanly(store: Store) -> None:
    base = await see(store, request("u1", "a1", "u2"))

    a, b = await asyncio.gather(
        see(store, request("u1", "a1", "u2", "a2", "path A")),
        see(store, request("u1", "a1", "u2", "a2", "path B")),
    )

    # One of them extends the existing branch, the other forks after the shared a2.
    assert {a.branch_id, b.branch_id} >= {base.branch_id}
    assert a.branch_id != b.branch_id
    assert sorted([a.new_branch, b.new_branch]) == [False, True]


def test_cache_control_inside_a_tool_input_is_real_data() -> None:
    # Only the API's own marker positions are cleaned; a tool's input is the user's data.
    plain = {"type": "tool_use", "id": "t", "name": "x", "input": {"a": 1}}
    with_key = {"type": "tool_use", "id": "t", "name": "x", "input": {"a": 1, "cache_control": "on"}}

    assert content_hash(plain) != content_hash(with_key)


async def test_a_busy_database_makes_identification_give_up_quickly(store: Store, tmp_path: Path) -> None:
    # Another process (e.g. a future `liber plan` command) holds SQLite's write lock.
    blocker = sqlite3.connect(tmp_path / "liber.db", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.perf_counter()
        results = await asyncio.gather(*(see(store, request(f"u{i}")) for i in range(3)), return_exceptions=True)
        elapsed = time.perf_counter() - started
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert all(isinstance(result, StoreBusy) for result in results)
    assert elapsed < 1.0, f"waited {elapsed:.2f}s"
