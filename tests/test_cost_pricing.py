"""Prices for plan cost estimates (liber/config.py, liber/plan/cost.py, ARCHITECTURE.md §6)."""

import json
from dataclasses import dataclass

import pytest

from liber.config import DEFAULT_PRICING, Settings
from liber.model.items import Operation, OperationKind, Plan
from liber.plan import cost


@dataclass(frozen=True)
class FakeItem:
    id: str
    kind: str
    msg_index: int
    tokens: int


def test_sonnet_5_5_has_its_own_entry() -> None:
    sonnet_5_5 = cost.price_for("claude-sonnet-5-5", DEFAULT_PRICING)
    sonnet_5 = cost.price_for("claude-sonnet-5", DEFAULT_PRICING)

    assert sonnet_5_5 is not None and (sonnet_5_5.input, sonnet_5_5.cache_write, sonnet_5_5.cache_read) == (
        2.0,
        2.5,
        0.10,
    )
    assert sonnet_5 is not None and sonnet_5.cache_read == 0.20  # the longest prefix decides


def test_haiku_5_5_charges_more_above_100k_tokens() -> None:
    haiku = DEFAULT_PRICING["claude-haiku-5-5"]

    short, long = haiku.rates_for(100_000), haiku.rates_for(100_001)

    assert (short.input, short.cache_write, short.cache_read) == (0.10, 0.125, 0.01)
    assert (long.input, long.cache_write, long.cache_read) == (0.50, 0.625, 0.05)
    assert DEFAULT_PRICING["claude-opus-5-5"].rates_for(900_000) is DEFAULT_PRICING["claude-opus-5-5"]  # no tier


def test_cost_prices_each_request_at_its_own_tier() -> None:
    # Deleting a 100k-token item takes the request from the long-prompt tier to the base one.
    items = [FakeItem("big", "text", 0, 100_000), FakeItem("rest", "text", 1, 50_000)]
    pending = Plan(operations=(Operation(kind=OperationKind.DELETE, item_ids=("big",)),))

    estimate = cost.estimate(items, Plan(), pending, DEFAULT_PRICING["claude-haiku-5-5"])

    assert (estimate.tokens_now, estimate.tokens_after) == (150_000, 50_000)
    assert estimate.saving_per_turn == pytest.approx((150_000 * 0.05 - 50_000 * 0.01) / 1_000_000)
    assert estimate.one_time_cost == pytest.approx(50_000 * (0.125 - 0.01) / 1_000_000)  # base rates after


def test_a_long_prompt_tier_can_be_set_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {
        "my-model": {
            "input": 1.0,
            "cache_write": 1.25,
            "cache_read": 0.1,
            "long_prompt": {"above_tokens": 1000, "input": 2.0, "cache_write": 2.5, "cache_read": 0.2},
        }
    }
    monkeypatch.setenv("LIBER_PRICING", json.dumps(table))

    price = Settings().pricing["my-model"]

    assert price.rates_for(1000).cache_read == 0.1 and price.rates_for(1001).cache_read == 0.2
