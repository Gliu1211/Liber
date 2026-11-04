"""What applying a pending plan version costs and saves (ARCHITECTURE.md §6, D6, ADR 0004 A4).

Before a compaction is applied, the user sees (`liber plan show`, later the dashboard):
- tokens per request now (applied version) and after (pending version);
- tokens reprocessed once: written to the prompt cache again because the history changed
  before them. A minor compaction (only appends after the frozen/live boundary) reprocesses from
  the old frozen boundary on; a major one (anything else) everything after the system prompt;
- the one-time cost of that, the saving per turn (fewer tokens read from the cache on every
  later request), and the payback period in turns;
- the reasoning dropped: every thinking block in the history when the version takes effect is
  stale from then on and left out (ADR 0012). That loss isn't priced; it's shown.

Everything is an estimate from the items' token estimates (D9), not from the content, so it
works from the database alone. Prices come from Settings.pricing (config.py), never from here.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from liber.config import ModelPrice
from liber.model.items import OperationKind, Plan
from liber.plan.policy import is_minor

POINTER_NOTE_TOKENS = 45  # about the size of an offload pointer note (plan/apply.py)


class ItemFacts(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def tokens(self) -> int: ...


@dataclass(frozen=True)
class Estimate:
    minor: bool
    tokens_now: int
    tokens_after: int
    reprocessed_tokens: int
    dropped_thinking_blocks: int
    dropped_thinking_tokens: int
    one_time_cost: float | None  # dollars; None without a price for the model
    saving_per_turn: float | None
    payback_turns: int | None  # None: no price, or it never pays back


def price_for(model: str | None, pricing: dict[str, ModelPrice]) -> ModelPrice | None:
    """The price of the longest matching model prefix, if any."""
    matches = [prefix for prefix in pricing if model is not None and model.startswith(prefix)]
    return pricing[max(matches, key=len)] if matches else None


def estimate(items: Iterable[ItemFacts], applied: Plan, pending: Plan, price: ModelPrice | None) -> Estimate:
    items = list(items)
    minor = is_minor(applied, pending)
    tokens_now = rendered_tokens(items, applied)
    tokens_after = rendered_tokens(items, pending)
    reprocessed = tokens_after - frozen_tokens(applied) if minor else tokens_after
    removed = removed_ids(pending)
    stale = [item for item in items if item.kind == "thinking" and item.id not in removed]

    one_time = saving = None
    payback = None
    if price is not None:
        # Reprocessed tokens would have been cache reads; now they are cache writes, once.
        one_time = reprocessed * (price.cache_write - price.cache_read) / 1_000_000
        saving = (tokens_now - tokens_after) * price.cache_read / 1_000_000
        payback = math.ceil(one_time / saving) if saving > 0 else None
    return Estimate(
        minor=minor,
        tokens_now=tokens_now,
        tokens_after=tokens_after,
        reprocessed_tokens=max(reprocessed, 0),
        dropped_thinking_blocks=len(stale),
        dropped_thinking_tokens=sum(item.tokens for item in stale),
        one_time_cost=one_time,
        saving_per_turn=saving,
        payback_turns=payback,
    )


def removed_ids(plan: Plan) -> set[str]:
    """Items a plan takes out of the history: summarized and deleted ones."""
    kinds = (OperationKind.SUMMARIZE, OperationKind.DELETE)
    return {item_id for op in plan.operations if op.kind in kinds for item_id in op.item_ids}


def frozen_tokens(plan: Plan) -> int:
    return sum(math.ceil(len(op.summary or "") / 4) for op in plan.operations if op.kind is OperationKind.SUMMARIZE)


def rendered_tokens(items: list[ItemFacts], plan: Plan) -> int:
    """Estimated message tokens of a request rendered with `plan` (system and tools not included)."""
    removed = removed_ids(plan)
    offloaded = {
        item_id for op in plan.operations if op.kind is OperationKind.OFFLOAD for item_id in op.item_ids
    } - removed
    kept = sum(item.tokens for item in items if item.id not in removed and item.id not in offloaded)
    return kept + POINTER_NOTE_TOKENS * len(offloaded) + frozen_tokens(plan)
