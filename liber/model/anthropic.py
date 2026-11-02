"""parse() and render() for the Anthropic Messages API (ARCHITECTURE.md §4 "Format adapters", D2).

These two functions are the only code that knows the Messages API's shape:

    parse(request)        -> ParsedRequest   items (one per content block) + what's needed to
                                             rebuild the request around them
    render(parsed, plan)  -> request         the request with the plan applied

The rule that makes Liber safe: with an empty plan, render(parse(r)) == r, exactly, with the
same key order (tested on every fixture and on generated requests). Everything Liber doesn't
edit is carried through untouched, by reference: top-level fields (model, system, tools, ...),
message-level fields, and each content block.

Applying plan operations (offload, delete, summarize -> frozen blocks) is Session 4's work, in
plan/apply.py; until then render() accepts only the empty plan.
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

# Rough token estimates for blocks whose JSON length says little about their token count.
IMAGE_TOKENS_ESTIMATE = 1600  # a typical ~1 megapixel image
BINARY_DOCUMENT_TOKENS_ESTIMATE = 3000  # a short PDF


class UnsupportedPlan(Exception):
    """render() was given plan operations it can't apply yet."""


@dataclass(frozen=True)
class MessageFrame:
    """One message of the request, minus its content (which lives in the items)."""

    index: int
    role: Role
    message: dict[str, Any]  # the original message dict; render keeps its other fields and key order
    content_is_string: bool  # the client wrote "content": "..." rather than a list of blocks
    chain_hash: str  # h_i: identifies this message and everything before it (D7)


@dataclass(frozen=True)
class ParsedRequest:
    original: dict[str, Any]  # the request as received; never modified
    root_hash: str  # h_0 = H(system || tools)
    messages: list[MessageFrame]
    items: list[Item]  # every content block, in conversation order

    @property
    def chain_hashes(self) -> list[str]:
        return [frame.chain_hash for frame in self.messages]


def parse(request: Any) -> ParsedRequest:
    """Split a Messages API request into items. Raises ValueError if it isn't request-shaped."""
    if not isinstance(request, dict):
        raise ValueError("the request is not a JSON object")
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise ValueError("the request has no `messages` list")

    root = chain.root_hash(request.get("system"), request.get("tools"))
    frames: list[MessageFrame] = []
    items: list[Item] = []
    tool_use_items: dict[str, str] = {}  # tool_use id -> item id, so results can point at their call
    previous_hash = root

    for index, message in enumerate(messages):
        role, blocks, content_is_string = read_message(index, message)
        canonical = [chain.canonical_block(block) for block in blocks]
        block_hashes = [chain.sha256_hex(text) for text in canonical]
        previous_hash = chain.message_hash(previous_hash, message, block_hashes)
        frames.append(MessageFrame(index, role, message, content_is_string, previous_hash))

        for block_index, block in enumerate(blocks):
            kind = KIND_BY_BLOCK_TYPE.get(str(block.get("type")), Kind.OTHER)
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
                    tokens=estimate_tokens(kind, block, canonical[block_index]),
                    tool_use_id=tool_use_id,
                    depends_on=tool_use_items.get(tool_use_id) if kind is Kind.TOOL_RESULT and tool_use_id else None,
                    block=block,
                )
            )
    return ParsedRequest(original=request, root_hash=root, messages=frames, items=items)


def read_message(index: int, message: Any) -> tuple[Role, list[dict[str, Any]], bool]:
    """A message's role, its content as a list of blocks, and whether it was written as a string."""
    if not isinstance(message, dict) or "content" not in message:
        raise ValueError(f"messages[{index}] is not a message object with content")
    role = Role(str(message.get("role")))  # ValueError for a missing role or one Liber doesn't know
    content = message["content"]
    if isinstance(content, str):
        return role, [{"type": "text", "text": content}], True
    if isinstance(content, list) and all(isinstance(block, dict) for block in content):
        return role, content, False
    raise ValueError(f"messages[{index}].content must be a string or a list of content blocks")


def pairing_id(block: dict[str, Any], kind: Kind) -> str | None:
    """The id that pairs a tool_use with its tool_result (§5), if this block has one."""
    key = {Kind.TOOL_USE: "id", Kind.TOOL_RESULT: "tool_use_id"}.get(kind)
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
