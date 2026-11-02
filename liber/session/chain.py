"""Session identification with a message hash chain (ARCHITECTURE.md D7).

The client resends the whole conversation on every request, and nothing in a request says
which conversation it belongs to. So Liber recognises conversations by their content, the way
git recognises commits:

    h_0 = H(system || tools)                      the "root" a conversation grows from
    h_i = H(h_{i-1} || canonical(message_i))      one hash per message

h_i identifies message i *and everything before it*. On each request Liber computes these
hashes, finds the deepest one it has stored (the longest known prefix), and treats the rest as
new. Conversations form a tree:

- the request continues a stored branch       -> same branch, new messages appended
- it leaves a branch part-way (an edited prompt, a client that trimmed old messages)
                                               -> a new branch forked at the last shared message
- nothing matches beyond the root (a new chat, or the client compacted its whole history)
                                               -> a new session

Canonicalization makes the hash ignore things that don't change the conversation: JSON key
order and spacing, `cache_control` markers (Claude Code moves its marker to the newest message
every turn), and the string-vs-block spelling of the same text.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection

from liber.session.store import (
    BranchRecord,
    ChainNode,
    ItemRecord,
    SessionRecord,
    Store,
    deepest_known_node,
    insert_new,
    touch_branch,
)

if TYPE_CHECKING:  # only for type hints; avoids a circular import at runtime
    from liber.model.anthropic import ParsedRequest


# --- Canonical form and hashes ----------------------------------------------------------------


# One shared encoder: json.dumps() with options builds a new encoder on every call, which was a
# measurable share of parse() time (a few hundred calls per request).
CANONICAL_ENCODER = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no spaces, unicode kept as-is. Equal values give equal text."""
    return CANONICAL_ENCODER.encode(value)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def without_cache_control(block: dict[str, Any]) -> dict[str, Any]:
    """A content block (or tool definition) without its `cache_control` marker.

    Markers can also sit on the blocks inside a tool_result, so those are cleaned too. Only these
    known places are cleaned, so a tool *input* that happens to contain a "cache_control" key is
    left alone.
    """
    is_tool_result = block.get("type") == "tool_result"
    if "cache_control" not in block and not is_tool_result:
        return block  # nothing to remove (the common case): no copy needed
    clean = {key: value for key, value in block.items() if key != "cache_control"}
    if is_tool_result and isinstance(block.get("content"), list):
        clean["content"] = [
            without_cache_control(inner) if isinstance(inner, dict) else inner for inner in block["content"]
        ]
    return clean


def canonical_block(block: dict[str, Any]) -> str:
    return canonical_json(without_cache_control(block))


def content_hash(block: dict[str, Any]) -> str:
    """SHA-256 of a block's canonical form: identical content has the same hash anywhere (§7)."""
    return sha256_hex(canonical_block(block))


def as_blocks(content: Any) -> list[Any]:
    """The API accepts "hi" as shorthand for [{"type": "text", "text": "hi"}]; hash them alike."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def root_hash(system: Any, tools: Any) -> str:
    """h_0: the system prompt and tool definitions a conversation starts from."""
    system_blocks = [without_cache_control(block) for block in as_blocks(system)]
    tool_definitions = [without_cache_control(tool) for tool in tools] if isinstance(tools, list) else []
    return sha256_hex(canonical_json({"system": system_blocks, "tools": tool_definitions}))


def message_hash(previous_hash: str, message: dict[str, Any], block_hashes: list[str]) -> str:
    """h_i = H(h_{i-1} || canonical(message_i)).

    A message is canonicalized as its fields (role, and any extras) with its content replaced by
    the content hashes of its blocks. That identifies the message exactly, and reuses the block
    hashes parse() computes anyway instead of serializing the message a second time.
    """
    fields = {key: value for key, value in message.items() if key not in ("content", "cache_control")}
    fields["content"] = block_hashes
    # previous_hash is always 64 hex characters, so plain concatenation is unambiguous.
    return sha256_hex(previous_hash + canonical_json(fields))


# --- Identification ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """Which conversation a request belongs to, and how much of it Liber already knew."""

    session_id: str | None  # None only for a request with no messages
    branch_id: str | None
    known_messages: int  # length of the longest stored prefix
    new_messages: int  # messages after it, registered by this request
    new_session: bool = False
    new_branch: bool = False
    parent_branch_id: str | None = None  # for a new branch: the branch it forked from
    fork_depth: int = 0  # for a new branch: messages it shares with its parent


def new_id() -> str:
    return uuid.uuid4().hex


async def identify(store: Store, parsed: "ParsedRequest") -> Identity:
    """Find the request's conversation (longest known prefix), then register what is new.

    The database work is one transaction on a worker thread (Store.run), under a lock so two
    simultaneous requests can't both decide to create the same branch.
    """
    if not parsed.chain_hashes:
        return Identity(session_id=None, branch_id=None, known_messages=0, new_messages=0)
    now = datetime.now(UTC)
    async with store.lock:
        return await store.run(lambda db: find_and_register(db.connection(), parsed, now))


def find_and_register(db: Connection, parsed: "ParsedRequest", now: datetime) -> Identity:
    """The body of identify(): a few plain SQL statements inside one transaction."""
    hashes = parsed.chain_hashes
    new_session = new_branch = False
    deepest = deepest_known_node(db, hashes)

    if deepest is None:
        # Nothing matches beyond the root: a new conversation with its first branch.
        known, session_id = 0, new_id()
        branch_id, parent_branch_id, fork_depth = new_id(), None, 0
        insert_new(db, SessionRecord, [{"id": session_id, "root_hash": parsed.root_hash, "created_at": now}])
        new_session = new_branch = True
    else:
        known, session_id = deepest.depth, deepest.session_id
        branch_id, parent_branch_id, fork_depth = deepest.branch_id, deepest.parent_branch_id, deepest.fork_depth
        if known < len(hashes) and deepest.head_hash != deepest.hash:
            # The branch goes on differently after the shared prefix: fork a new branch here.
            branch_id, parent_branch_id, fork_depth = new_id(), deepest.branch_id, known
            new_branch = True

    if new_branch:
        branch = {
            "id": branch_id,
            "session_id": session_id,
            "parent_branch_id": parent_branch_id,
            "fork_depth": fork_depth,
            "head_hash": hashes[-1],
            "head_depth": len(hashes),
            "created_at": now,
            "last_seen_at": now,
        }
        insert_new(db, BranchRecord, [branch])
    else:
        touch_branch(db, branch_id, now, new_head=(hashes[-1], len(hashes)) if known < len(hashes) else None)

    if known < len(hashes):
        register_new_messages(db, parsed, known, session_id, branch_id, now)

    return Identity(
        session_id=session_id,
        branch_id=branch_id,
        known_messages=known,
        new_messages=len(hashes) - known,
        new_session=new_session,
        new_branch=new_branch,
        parent_branch_id=parent_branch_id,
        fork_depth=fork_depth,
    )


def register_new_messages(
    db: Connection, parsed: "ParsedRequest", known: int, session_id: str, branch_id: str, now: datetime
) -> None:
    """Store a chain node for each new message, and an item row for each of their blocks."""
    nodes = [
        {
            "hash": frame.chain_hash,
            "parent_hash": parsed.messages[frame.index - 1].chain_hash if frame.index > 0 else parsed.root_hash,
            "session_id": session_id,
            "branch_id": branch_id,
            "depth": frame.index + 1,
            "role": frame.role.value,
            "created_at": now,
        }
        for frame in parsed.messages[known:]
    ]
    items = [
        {
            "id": item.id,
            "session_id": session_id,
            "node_hash": parsed.messages[item.msg_index].chain_hash,
            "msg_index": item.msg_index,
            "block_index": item.block_index,
            "role": item.role.value,
            "kind": item.kind.value,
            "content_hash": item.content_hash,
            "tokens": item.tokens,
            "tokens_exact": item.tokens_exact,
            "topic": item.topic,
            "tool_use_id": item.tool_use_id,
            "depends_on": item.depends_on,
            "created_at": now,
        }
        for item in parsed.items
        if item.msg_index >= known
    ]
    insert_new(db, ChainNode, nodes)
    insert_new(db, ItemRecord, items)
