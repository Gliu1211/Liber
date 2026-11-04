"""The context model: items, operations and plans (ARCHITECTURE.md §4).

Why it exists: every other part of Liber (identification, the dashboard, plans, the worker)
talks about a conversation as a list of *items*, one per content block, instead of the raw
Messages API JSON. Only model/anthropic.py knows the wire format; everything else uses these
types, so an OpenAI adapter can later produce the same items (D2).

All of these are frozen: an Item describes a block of a request as it arrived and is never
edited in place. Edits are expressed as a Plan, applied when rendering (Session 4).

Item is a plain dataclass, while Operation and Plan are pydantic models: items are built in bulk
on the request path from values Liber computes itself, so validating them would only cost time
(it was the largest share of parse()). Plans arrive from users and the dashboard, so they are
validated.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"  # a mid-conversation system message inside `messages`


class Kind(StrEnum):
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    IMAGE = "image"
    DOCUMENT = "document"
    THINKING = "thinking"  # thinking and redacted_thinking: never modified (§5)
    OTHER = "other"  # anything else, e.g. server tool blocks or block types added later


@dataclass(frozen=True, slots=True, kw_only=True)
class Item:
    """One content block of the client's history (§4)."""

    # Stable id: the chain hash of the block's message (which covers everything before it)
    # plus the block's index. Same block at the same point of a conversation -> same id.
    id: str
    msg_index: int  # position of the block's message in `messages`
    block_index: int  # position of the block in that message's content
    role: Role
    kind: Kind
    content_hash: str  # SHA-256 of the canonical block: same content anywhere -> same hash
    tokens: int  # a fast local estimate until the worker supplies the exact count (D9)
    block: dict[str, Any]  # the content block exactly as the client sent it
    tokens_exact: bool = False
    topic: str | None = None  # set later by the worker (D10)
    tool_use_id: str | None = None  # tool_use / tool_result: the id that pairs them
    depends_on: str | None = None  # tool_result -> the item id of its tool_use


class OperationKind(StrEnum):
    KEEP = "keep"  # the default; listed for completeness
    SUMMARIZE = "summarize"  # a range of whole exchanges -> one frozen block (D5, D8)
    OFFLOAD = "offload"  # content -> pointer note, original stored on disk by hash
    DELETE = "delete"  # remove (expands to whole tool pairs / exchanges)


class Operation(BaseModel):
    """One edit applied to an item, or to a contiguous range of items in conversation order."""

    model_config = ConfigDict(frozen=True)

    kind: OperationKind
    item_ids: tuple[str, ...]
    note: str | None = None  # e.g. the label shown in an offload pointer note
    # summarize only: the frozen block's text, fixed when the plan version is created, so every
    # request renders exactly the same bytes for it (ADR 0004 A2).
    summary: str | None = None


class Plan(BaseModel):
    """An ordered list of operations for one session (§4).

    Stored as immutable, numbered versions (plan/versions.py): a pending version becomes the
    applied one at a break point (§6), and undo goes back to the previous one. The empty plan
    (version 0) leaves every request unchanged.
    """

    model_config = ConfigDict(frozen=True)

    operations: tuple[Operation, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.operations
