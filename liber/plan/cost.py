"""What applying a pending plan version costs and saves (ARCHITECTURE.md §6, D6, ADR 0004 A4).

Before a compaction is applied, the user sees (`liber plan show`, the dashboard):
- tokens per request now (applied version) and after (pending version), thinking not counted
  (see below);
- tokens reprocessed once: written to the prompt cache again because the history changed
  before them. A minor compaction (only appends after the frozen/live boundary) reprocesses from
  the old frozen boundary on; a major one (anything else) everything after the system prompt;
- the one-time cost of that, the saving per turn (fewer tokens read from the cache on every
  later request), and the payback period in turns;
- the reasoning dropped: every thinking block in the history when the version takes effect is
  stale from then on and left out (ADR 0012). That loss isn't priced; it's shown.

Why thinking isn't counted in the sizes: a thinking block's estimate is its length, signature
included, but in the 2026-11-08 Tier 2 run a request with every earlier thinking block left out
cost about as much as the full request (E vs A on derived_pool_load). Counting dropped thinking as
a saving made plans look smaller than they were, and let `liber plan create` accept plans that
grew the request (owner, 2026-11-09). Until a real count says otherwise, the sizes compare
everything else, and the thinking left out is listed on its own.

Everything is an estimate from the items' token estimates (D9), not from the content, so it
works from the database alone. Where the worker has an exchange's exact count, its items' estimates
are scaled to match it (`with_exact_counts`). Prices come from Settings.pricing (config.py), never from here;
a model with a long-prompt tier is priced per request by its estimated message tokens (system
and tools aren't counted, so a request just over the threshold may be priced at the lower rates).
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from liber.config import ModelPrice
from liber.model.items import OperationKind, Plan
from liber.plan.policy import is_minor

if TYPE_CHECKING:  # only for type hints
    from liber.cache.exchanges import Exchange

POINTER_NOTE_TOKENS = 45  # about the size of an offload pointer note (plan/apply.py)


class ItemFacts(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def msg_index(self) -> int: ...

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


@dataclass(frozen=True)
class CountedItem:
    """An item's facts with its token count corrected by its exchange's exact count."""

    id: str
    kind: str
    msg_index: int
    tokens: int


def with_exact_counts(
    items: Sequence[ItemFacts], exchanges: Sequence["Exchange[Any]"], exact: dict[str, int]
) -> list[ItemFacts]:
    """`items`, with each exactly counted exchange's estimates scaled to its exact count.

    `exact`: counts by exchange key. Counts leave out thinking and mid-conversation system messages
    (worker/tokens.py), so those items keep their estimates. The shares add up to the exact count.
    """
    corrected: dict[str, int] = {}
    for exchange in exchanges:
        counted = [item for item in exchange.items if item.kind != "thinking" and item.role != "system"]
        if exchange.key not in exact or not counted:
            continue
        estimated = sum(item.tokens for item in counted)
        remaining = exact[exchange.key]
        for number, item in enumerate(counted):
            share = item.tokens / estimated if estimated else 1 / len(counted)
            # The last item takes what's left, so rounding never changes the total.
            tokens = remaining if number == len(counted) - 1 else min(round(exact[exchange.key] * share), remaining)
            corrected[item.id] = tokens
            remaining -= tokens
    return [
        CountedItem(item.id, item.kind, item.msg_index, corrected[item.id]) if item.id in corrected else item
        for item in items
    ]


def price_for(model: str | None, pricing: dict[str, ModelPrice]) -> ModelPrice | None:
    """The price of the longest matching model prefix, if any."""
    matches = [prefix for prefix in pricing if model is not None and model.startswith(prefix)]
    return pricing[max(matches, key=len)] if matches else None


def estimate(
    items: Iterable[ItemFacts], applied: Plan, pending: Plan, price: ModelPrice | None, activated_at: int | None = None
) -> Estimate:
    """`activated_at`: where the applied version took effect, if any (thinking before it is already left out)."""
    items = list(items)
    minor = is_minor(applied, pending)
    removed = removed_ids(pending)
    # Thinking sent today: produced after the applied version took effect (all of it, if none has).
    thinking_now = [i for i in items if i.kind == "thinking" and i.msg_index >= (activated_at or 0)]
    # Once the pending version takes effect, every thinking block in the history is stale (ADR 0012).
    stale = [i for i in thinking_now if i.id not in removed]
    # Sizes without thinking on either side (module docstring); the dropped reasoning is listed apart.
    thinking = {i.id for i in items if i.kind == "thinking"}
    tokens_now = rendered_tokens(items, applied, thinking)
    tokens_after = rendered_tokens(items, pending, thinking)
    reprocessed = tokens_after - frozen_tokens(applied) if minor else tokens_after

    one_time = saving = None
    payback = None
    if price is not None:
        # Each request pays the rates for its size (some models charge more for long prompts).
        rates_now, rates_after = price.rates_for(tokens_now), price.rates_for(tokens_after)
        # Reprocessed tokens would have been cache reads; now they are cache writes, once.
        one_time = max(reprocessed, 0) * (rates_after.cache_write - rates_after.cache_read) / 1_000_000
        saving = (tokens_now * rates_now.cache_read - tokens_after * rates_after.cache_read) / 1_000_000
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


def rendered_tokens(items: list[ItemFacts], plan: Plan, left_out: set[str]) -> int:
    """Estimated message tokens of a request rendered with `plan` (system and tools not included).

    `left_out`: thinking items the request leaves out as stale (ADR 0012).
    """
    removed = removed_ids(plan) | left_out
    offloaded = {
        item_id for op in plan.operations if op.kind is OperationKind.OFFLOAD for item_id in op.item_ids
    } - removed
    kept = sum(item.tokens for item in items if item.id not in removed and item.id not in offloaded)
    return kept + POINTER_NOTE_TOKENS * len(offloaded) + frozen_tokens(plan)
