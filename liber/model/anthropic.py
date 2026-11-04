"""parse() and render() for the Anthropic Messages API (ARCHITECTURE.md §4 "Format adapters", D2).

These two functions are the only code that knows the Messages API's shape:

    parse(request, previous)  -> ParsedRequest   items (one per content block) + what's needed to
                                                 rebuild the request around them
    render(parsed, plan)      -> request         the request with the plan applied

The rule that makes Liber safe: with an empty plan, render(parse(r)) == r, exactly, with the
same key order (tested on every fixture and on generated requests). Everything Liber doesn't
edit is carried through untouched, by reference: top-level fields (model, system, tools, ...),
message-level fields, and each content block.

Applying plan operations (offload, delete, summarize -> frozen blocks) is Session 4's work, in
plan/apply.py; until then render() accepts only the empty plan.

Speed (ARCHITECTURE.md §3 latency budget): a client resends the whole conversation on every
turn, and canonicalizing and hashing every block is most of parse()'s time. So parse() can be
given the previous request of the same conversation (`RecentRequests` keeps them) and copies
its results for the messages that haven't changed. Only new or changed messages are hashed.
The result is exactly what parsing from scratch gives (docs/decisions/0007).
"""

import math
from dataclasses import dataclass
from typing import Any

from liber.model.items import Item, Kind, Plan, Role
from liber.session import chain

KIND_BY_BLOCK_TYPE = {
    "text": Kind.TEXT,
    "tool_use": Kind.TOOL_USE,
    "tool_result": Kind.TOOL_RESULT,
    "image": Kind.IMAGE,
    "document": Kind.DOCUMENT,
    "thinking": Kind.THINKING,
    "redacted_thinking": Kind.THINKING,
}

ROLE_VALUES = {role.value for role in Role}

# The field holding the id that pairs a tool_use with its tool_result (§5).
PAIRING_KEYS = {Kind.TOOL_USE: "id", Kind.TOOL_RESULT: "tool_use_id"}

# Conversations whose latest request RecentRequests keeps. Claude Code runs its main conversation
# alongside subagents and short side requests; a handful of slots keeps the busy ones.
RECENT_CONVERSATIONS = 8

# Rough token estimates for blocks whose JSON length says little about their token count.
IMAGE_TOKENS_ESTIMATE = 1600  # a typical ~1 megapixel image
BINARY_DOCUMENT_TOKENS_ESTIMATE = 3000  # a short PDF


class UnsupportedPlan(Exception):
    """render() was given plan operations it can't apply yet."""


class RequestShapeError(ValueError):
    """The request isn't shaped like a Messages API request. Messages never quote its content."""


@dataclass(frozen=True)
class MessageFrame:
    """One message of the request, minus its content (which lives in the items)."""

    index: int
    role: Role
    message: dict[str, Any]  # the original message dict; render keeps its other fields and key order
    content_is_string: bool  # the client wrote "content": "..." rather than a list of blocks
    chain_hash: str  # h_i: identifies this message and everything before it (D7)
    # What parse() computed for the message's blocks, kept so the next request can reuse it
    # (the same values are in the items, one per block).
    block_hashes: tuple[str, ...]
    block_tokens: tuple[int, ...]
    has_numbers: bool  # holds a number or true/false: == isn't enough to spot it unchanged


@dataclass(frozen=True)
class ParsedRequest:
    original: dict[str, Any]  # the request as received; never modified
    root_hash: str  # h_0 = H(system || tools)
    messages: list[MessageFrame]
    items: list[Item]  # every content block, in conversation order

    @property
    def chain_hashes(self) -> list[str]:
        return [frame.chain_hash for frame in self.messages]


class RecentRequests:
    """The latest parsed request of each of the last few conversations, for parse(previous=...).

    One per process (the app keeps it in app.state), in memory only. Picking an unrelated entry
    can't give a wrong result, only less reuse, because parse() checks everything it reuses.
    Memory: one decoded request per conversation kept (a ~1 MB request is a few MB of Python
    objects), at most RECENT_CONVERSATIONS of them.
    """

    def __init__(self, size: int = RECENT_CONVERSATIONS) -> None:
        self.size = size
        self.entries: list[ParsedRequest] = []  # most recently used first

    def find(self, request: Any) -> ParsedRequest | None:
        """The latest request that starts like this one: same first message, system and tools.

        Those don't change while a conversation goes on, and each comparison is a cheap == that
        usually stops at the first difference.
        """
        messages = request.get("messages") if isinstance(request, dict) else None
        if not isinstance(messages, list) or not messages:
            return None
        for entry in self.entries:
            earlier = entry.original
            if (
                earlier["messages"][0] == messages[0]
                and earlier.get("system") == request.get("system")
                and earlier.get("tools") == request.get("tools")
            ):
                return entry
        return None

    def remember(self, parsed: ParsedRequest, replacing: ParsedRequest | None = None) -> None:
        """Keep `parsed` as the newest entry, in place of `replacing` (its conversation's older request).

        The least recently used entry is dropped once there are more than `size`.
        """
        if not parsed.messages:
            return  # nothing a later request could reuse
        # `is not`, not !=: comparing two parsed requests with != would compare all their contents.
        others = [entry for entry in self.entries if entry is not replacing]
        self.entries = [parsed, *others][: self.size]


def parse(request: Any, previous: ParsedRequest | None = None) -> ParsedRequest:
    """Split a Messages API request into items. Raises RequestShapeError if it isn't request-shaped.

    `previous`: an earlier parse to reuse work from, normally the latest request of the same
    conversation (see RecentRequests). A message gets previous's results when it is the same
    message after the same history (reusable_frame checks both). Any other `previous`, or
    None, just means more hashing: the result is the same either way.
    """
    if not isinstance(request, dict):
        raise RequestShapeError("the request is not a JSON object")
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise RequestShapeError("the request has no `messages` list")

    root = chain.root_hash(request.get("system"), request.get("tools"))
    frames: list[MessageFrame] = []
    items: list[Item] = []
    tool_use_items: dict[str, str] = {}  # tool_use id -> item id, so results can point at their call
    previous_hash = root

    for index, message in enumerate(messages):
        role, blocks, content_is_string = read_message(index, message)
        known = reusable_frame(previous, index, previous_hash, message)
        if known is not None:
            # Unchanged since the previous request: copy its results instead of rehashing.
            block_hashes, block_tokens, has_numbers = known.block_hashes, known.block_tokens, known.has_numbers
            previous_hash = known.chain_hash
        else:
            canonical = [chain.canonical_block(block) for block in blocks]
            block_hashes = tuple(chain.sha256_hex(text) for text in canonical)
            block_tokens = tuple(
                estimate_tokens(block_kind(block), block, text) for block, text in zip(blocks, canonical, strict=True)
            )
            has_numbers = chain.has_numbers(message)
            previous_hash = chain.message_hash(previous_hash, message, list(block_hashes))
        frames.append(
            MessageFrame(
                index, role, message, content_is_string, previous_hash, block_hashes, block_tokens, has_numbers
            )
        )

        # Items are rebuilt even for reused messages: each points at this request's block object.
        for block_index, block in enumerate(blocks):
            kind = block_kind(block)
            item_id = f"{previous_hash[:16]}-{block_index}"
            tool_use_id = pairing_id(block, kind)
            if kind is Kind.TOOL_USE and tool_use_id is not None:
                tool_use_items[tool_use_id] = item_id
            items.append(
                Item(
                    id=item_id,
                    msg_index=index,
                    block_index=block_index,
                    role=role,
                    kind=kind,
                    content_hash=block_hashes[block_index],
                    tokens=block_tokens[block_index],
                    tool_use_id=tool_use_id,
                    depends_on=tool_use_items.get(tool_use_id) if kind is Kind.TOOL_RESULT and tool_use_id else None,
                    block=block,
                )
            )
    return ParsedRequest(original=request, root_hash=root, messages=frames, items=items)


def reusable_frame(
    previous: ParsedRequest | None, index: int, hash_before: str, message: dict[str, Any]
) -> MessageFrame | None:
    """previous's frame for messages[index], if everything parse() computed for it still holds.

    That needs the same history before the message (equal chain hashes: h_i covers everything
    before message i) and the same message. Comparing the decoded messages with == runs in C and
    is far cheaper than canonicalizing and hashing them (~0.2 ms against ~6 ms per MB). For JSON
    without numbers, == means exactly "same canonical form"; with numbers it doesn't (Python
    says 1 == 1.0 == True), so those messages are compared with chain.same_json instead.
    """
    if previous is None or index >= len(previous.messages):
        return None
    known = previous.messages[index]
    known_hash_before = previous.messages[index - 1].chain_hash if index > 0 else previous.root_hash
    if known_hash_before != hash_before:
        return None
    same = chain.same_json(known.message, message) if known.has_numbers else known.message == message
    return known if same else None


def block_kind(block: dict[str, Any]) -> Kind:
    return KIND_BY_BLOCK_TYPE.get(str(block.get("type")), Kind.OTHER)


def read_message(index: int, message: Any) -> tuple[Role, list[dict[str, Any]], bool]:
    """A message's role, its content as a list of blocks, and whether it was written as a string."""
    if not isinstance(message, dict) or "content" not in message:
        raise RequestShapeError(f"messages[{index}] is not a message object with content")
    if message.get("role") not in ROLE_VALUES:
        raise RequestShapeError(f"messages[{index}] has a missing or unknown role")  # not echoed: it's client data
    role = Role(message["role"])
    content = message["content"]
    if isinstance(content, str):
        return role, [{"type": "text", "text": content}], True
    if isinstance(content, list) and all(isinstance(block, dict) for block in content):
        return role, content, False
    raise RequestShapeError(f"messages[{index}].content must be a string or a list of content blocks")


def pairing_id(block: dict[str, Any], kind: Kind) -> str | None:
    """The id that pairs a tool_use with its tool_result (§5), if this block has one."""
    key = PAIRING_KEYS.get(kind)
    value = block.get(key) if key else None
    return value if isinstance(value, str) else None


def estimate_tokens(kind: Kind, block: dict[str, Any], canonical: str) -> int:
    """A fast, rough token count; the worker replaces it with an exact one (D9).

    About four characters per token for text-like content. Images and binary documents are
    base64, whose length doesn't track tokens, so they get a typical fixed size instead.
    """
    if kind is Kind.IMAGE:
        return IMAGE_TOKENS_ESTIMATE
    source = block.get("source")
    if kind is Kind.DOCUMENT and isinstance(source, dict) and source.get("type") != "text":
        return BINARY_DOCUMENT_TOKENS_ESTIMATE
    return max(1, math.ceil(len(canonical) / 4))


def render(parsed: ParsedRequest, plan: Plan) -> dict[str, Any]:
    """Rebuild the request from its items, applying `plan` (empty plans only, until Session 4)."""
    if not plan.is_empty:
        raise UnsupportedPlan("plan operations are applied from Session 4 (plan/apply.py)")

    blocks_by_message: list[list[dict[str, Any]]] = [[] for _ in parsed.messages]
    for item in parsed.items:
        blocks_by_message[item.msg_index].append(item.block)

    messages = [render_message(frame, blocks) for frame, blocks in zip(parsed.messages, blocks_by_message, strict=True)]
    rendered = dict(parsed.original)  # same top-level keys in the same order...
    rendered["messages"] = messages  # ...with only the messages rebuilt
    return rendered


def render_message(frame: MessageFrame, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    content: str | list[dict[str, Any]] = blocks
    # Give back the client's "content": "..." shorthand if the message is still one plain text block.
    if frame.content_is_string and len(blocks) == 1 and blocks[0].keys() == {"type", "text"}:
        content = blocks[0]["text"]
    # Rebuild the message with its keys in the original order, swapping in the new content.
    return {key: (content if key == "content" else value) for key, value in frame.message.items()}
