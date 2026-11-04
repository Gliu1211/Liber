"""Apply an edit plan to a parsed request (ARCHITECTURE.md §4 operations, D5/D8, §5 boundaries).

model/anthropic.py's render() calls apply_plan() for any non-empty plan. Operations, in the
order they take effect:

    summarize  a range of whole exchanges from the start of the live region becomes a frozen
               block: a labelled text block placed at the front of the first live user
               message (D8). Each further summary appends a block (D5), so everything before
               it keeps its exact bytes and the provider's prefix cache still hits.
    delete     removes items; a tool call takes its result with it and vice versa, and a
               message left empty takes its whole exchange with it.
    offload    replaces an item's content with a pointer note; a tool_result keeps its block
               and tool_use_id, so the tool pairing stays valid (§4).

Operations on items inside a summarized range are superseded by the summary. A plan that
doesn't fit the request (it names messages this request doesn't have, a summary that doesn't
end at an exchange boundary, an edit of the current turn) raises PlanNotApplicable, and the
request goes through without the plan. Anything the operations get wrong is the validator's to
catch (§5); the pipeline then forwards the client's request.

Two promises (ADR 0007, ADR 0004 A2): the client's objects are never edited (changed messages
and blocks are new dicts), and the same history with the same plan renders to the same bytes.
Summary texts are fixed in the plan itself, so rendering never computes them.
"""

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol

from liber.model.items import Item, Kind, Operation, OperationKind, Plan
from liber.model.validate import tool_results_in

if TYPE_CHECKING:  # only for type hints; anthropic.py imports this module
    from liber.model.anthropic import ParsedRequest

OFFLOADABLE = {Kind.TEXT: "text", Kind.TOOL_RESULT: "tool result", Kind.IMAGE: "image", Kind.DOCUMENT: "document"}


class PlanNotApplicable(Exception):
    """The plan doesn't fit this request; it goes through without the plan. Not a Liber error."""


class ItemFacts(Protocol):
    """What summarize_operation needs to know about an item (an Item, or a stored ItemRecord)."""

    @property
    def id(self) -> str: ...

    @property
    def msg_index(self) -> int: ...

    @property
    def tokens(self) -> int: ...


def exchange_starts(messages: list[Any]) -> list[int]:
    """Indexes of the user messages that start an exchange (§5): the ones without tool results."""
    return [
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "user" and not tool_results_in(message)
    ]


def summarize_operation(items: Iterable[ItemFacts], first: int, last: int, block_number: int) -> Operation:
    """An operation summarizing messages first..last, with its frozen block's text.

    The text is a stub until summaries are written by the worker (Session 5). It is decided here,
    once, and stored with the plan version, so every request renders the same bytes for it.
    """
    covered = [item for item in items if first <= item.msg_index <= last]
    tokens = sum(item.tokens for item in covered)
    text = (
        f'<liber:summary block="{block_number}" covers="msgs {first}-{last}">'
        f"[{last - first + 1} earlier messages (about {tokens:,} tokens) were compacted by Liber. "
        "No written summary is available yet.]</liber:summary>"
    )
    return Operation(kind=OperationKind.SUMMARIZE, item_ids=tuple(item.id for item in covered), summary=text)


def pointer_note(item: Item, label: str) -> str:
    """The text that replaces offloaded content (§4)."""
    return (
        f'[Liber: offloaded item {item.content_hash[:12]} — "{label}", {item.tokens:,} tokens. '
        "Stored locally; restore from the Liber dashboard.]"
    )


def apply_plan(parsed: "ParsedRequest", plan: Plan) -> list[dict[str, Any]]:
    """The request's messages with `plan` applied. Raises PlanNotApplicable if it doesn't fit."""
    items = {item.id: item for item in parsed.items}
    for operation in plan.operations:
        if any(item_id not in items for item_id in operation.item_ids):
            raise PlanNotApplicable("the plan refers to messages this request doesn't have")

    originals = [frame.message for frame in parsed.messages]
    blocks: list[list[dict[str, Any]]] = [[] for _ in originals]  # each message's blocks, as parsed
    for item in parsed.items:
        blocks[item.msg_index].append(item.block)
    starts = exchange_starts(originals)
    current_turn = starts[-1] if starts else 0

    frozen, live_from = frozen_region(plan, items, blocks, starts)

    removed: set[tuple[int, int]] = set()  # (message, block) positions
    replaced: dict[tuple[int, int], dict[str, Any]] = {}
    for operation in plan.operations:
        targets = [items[item_id] for item_id in operation.item_ids if items[item_id].msg_index >= live_from]
        if operation.kind is OperationKind.DELETE:
            targets = with_tool_partners(targets, parsed.items)
        edits = operation.kind in (OperationKind.DELETE, OperationKind.OFFLOAD)
        if edits and any(item.msg_index >= current_turn for item in targets):
            raise PlanNotApplicable("the plan edits the latest exchange, the turn in progress; it is never edited")
        for item in targets:
            position = (item.msg_index, item.block_index)
            if operation.kind is OperationKind.DELETE:
                if item.kind is Kind.THINKING:
                    raise PlanNotApplicable("thinking blocks are never edited (§5)")
                removed.add(position)
            elif operation.kind is OperationKind.OFFLOAD:
                if item.kind not in OFFLOADABLE:
                    raise PlanNotApplicable(f"a {item.kind} block can't be offloaded")
                replaced[position] = offloaded_block(item, operation.note or OFFLOADABLE[item.kind])

    removed_messages = emptied_exchanges(removed, blocks, starts)
    messages: list[dict[str, Any]] = []
    for index in range(live_from, len(originals)):
        if index in removed_messages:
            continue
        kept = [
            replaced.get((index, block_index), block)
            for block_index, block in enumerate(blocks[index])
            if (index, block_index) not in removed
        ]
        if not messages and frozen:
            kept = [{"type": "text", "text": text} for text in frozen] + kept  # D8: new dicts each time
        if len(kept) == len(blocks[index]) and all(new is old for new, old in zip(kept, blocks[index], strict=True)):
            messages.append(originals[index])  # untouched: the client's own message, as sent
        else:
            # The message with its keys in the client's order and only its content swapped.
            messages.append({key: (kept if key == "content" else value) for key, value in originals[index].items()})
    return messages


def frozen_region(
    plan: Plan, items: dict[str, Item], blocks: list[list[dict[str, Any]]], starts: list[int]
) -> tuple[list[str], int]:
    """The frozen blocks' texts, in order, and the index of the first live message.

    Each summary must start where the live region starts and end just before a user message
    that starts a new exchange (§5 compaction boundaries), covering whole messages.
    """
    texts: list[str] = []
    live_from = 0
    for operation in plan.operations:
        if operation.kind is not OperationKind.SUMMARIZE:
            continue
        covered = sorted({items[item_id].msg_index for item_id in operation.item_ids})
        first, last = covered[0], covered[-1]
        # A message with no content blocks (an effort-only system message) has no item to name
        # it; one at the start of the live region goes with the summary.
        while first > live_from and not blocks[first - 1]:
            first -= 1
        if first != live_from:
            raise PlanNotApplicable("a summary must start where the live region starts")
        # Whole messages: every block from `first` to `last` is in the summary.
        if len(set(operation.item_ids)) != sum(len(blocks[index]) for index in range(first, last + 1)):
            raise PlanNotApplicable("a summary must cover whole messages")
        if last + 1 not in starts:
            raise PlanNotApplicable("a summary must end just before a user message that starts a new exchange")
        if operation.summary is None:
            raise PlanNotApplicable("a summary needs its text")
        texts.append(operation.summary)
        live_from = last + 1
    return texts, live_from


def with_tool_partners(targets: list[Item], all_items: list[Item]) -> list[Item]:
    """Deleting a tool call deletes its result, and the other way round (§4)."""
    chosen = {item.id for item in targets}
    for item in all_items:
        calls_a_target = item.depends_on in chosen  # a result of a chosen tool call
        answered_by_target = any(t.depends_on == item.id for t in targets)  # the call of a chosen result
        if calls_a_target or answered_by_target:
            chosen.add(item.id)
    return [item for item in all_items if item.id in chosen]


def emptied_exchanges(removed: set[tuple[int, int]], blocks: list[list[dict[str, Any]]], starts: list[int]) -> set[int]:
    """Messages to drop: every message of an exchange in which deleting left a message empty (§4)."""
    dropped: set[int] = set()
    for index, message_blocks in enumerate(blocks):
        if message_blocks and all((index, block_index) in removed for block_index in range(len(message_blocks))):
            begin = max([start for start in starts if start <= index], default=0)
            end = min([start for start in starts if start > index], default=len(blocks))
            dropped.update(range(begin, end))
    return dropped


def offloaded_block(item: Item, label: str) -> dict[str, Any]:
    """The block that takes an offloaded item's place: a new dict, never the client's."""
    note = pointer_note(item, label)
    if item.kind is Kind.TOOL_RESULT:
        block = dict(item.block)  # same keys in the same order...
        block["content"] = note  # ...with only the content replaced, so tool_use_id stays (§4)
        return block
    block = {"type": "text", "text": note}
    if "cache_control" in item.block:
        block["cache_control"] = item.block["cache_control"]  # §5: keep the client's marker
    return block
