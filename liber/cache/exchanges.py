"""What the worker knows about each exchange, by its content (ARCHITECTURE.md §7, §9, D9, D10).

A summary always covers whole exchanges (§5: a user message that starts a turn, through the
message before the next one), so the background worker works one exchange at a time:

    exchange key -> exact token count, per counting model       (table exchange_tokens)
    exchange key -> summary, topic (+ notes), per summarizer    (table exchange_summaries)

The key is a hash of the exchange's blocks (their content hashes and roles, in order), not of
its position: the same exchange in another branch or session reuses what was already written,
so identical content is never counted or summarized twice (§7). Only completed exchanges have
keys here; the latest one is the turn in progress, which is never summarized.

Items come from the request path (model/items.Item) or from the database (ItemRecord); both have
the fields `ItemFacts` names, so the CLI and the worker compute the same keys.
"""

import hashlib
import itertools
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Protocol, TypeVar, cast

from sqlalchemy import Connection, Table, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from liber.model.items import ExchangeSummary, KeptCode
from liber.session.store import ExchangeSummaryRecord, ExchangeTokensRecord


class ItemFacts(Protocol):
    @property
    def msg_index(self) -> int: ...

    @property
    def role(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def content_hash(self) -> str: ...


ItemT = TypeVar("ItemT", bound=ItemFacts)


@dataclass(frozen=True)
class Exchange(Generic[ItemT]):
    first: int  # index of its first message (a user message that starts a turn)
    last: int  # index of its last message
    items: tuple[ItemT, ...]
    key: str


def completed_exchanges(items: Iterable[ItemT]) -> list[Exchange[ItemT]]:
    """The conversation's exchanges, oldest first, without the latest one (the turn in progress).

    `items` in conversation order. One pass over them: sessions can have thousands of items.
    """
    items = list(items)
    tool_results = {item.msg_index for item in items if item.kind == "tool_result"}
    starts = sorted({item.msg_index for item in items if item.role == "user"} - tool_results)
    groups: list[list[ItemT]] = [[] for _ in starts]
    position = -1  # the exchange the current item belongs to (-1: before the first)
    for item in items:
        while position + 1 < len(starts) and item.msg_index >= starts[position + 1]:
            position += 1
        if position >= 0:
            groups[position].append(item)
    exchanges = []
    for number, (first, following) in enumerate(itertools.pairwise(starts)):
        covered = tuple(groups[number])
        exchanges.append(Exchange(first, following - 1, covered, exchange_key(covered, first)))
    return exchanges


def exchange_key(items: Sequence[ItemFacts], first: int) -> str:
    """Hash of what the exchange holds: each block's message (counted from the exchange's start), role and content."""
    described = [[item.msg_index - first, item.role, item.content_hash] for item in items]
    return hashlib.sha256(json.dumps(described).encode()).hexdigest()


def cached_tokens(db: Connection, keys: Iterable[str], model: str) -> dict[str, int]:
    table = ExchangeTokensRecord
    rows = db.execute(
        select(table.exchange_key, table.tokens).where(table.model == model, table.exchange_key.in_(set(keys)))
    )
    return {key: tokens for key, tokens in rows}


def record_tokens(db: Connection, key: str, model: str, tokens: int, now: datetime) -> None:
    values = {"tokens": tokens, "updated_at": now}
    db.execute(
        sqlite_insert(cast(Table, ExchangeTokensRecord.__table__))
        .values(exchange_key=key, model=model, **values)
        .on_conflict_do_update(index_elements=["exchange_key", "model"], set_=values)
    )


def cached_summaries(db: Connection, keys: Iterable[str], summarizer: str) -> dict[str, ExchangeSummary]:
    table = ExchangeSummaryRecord
    rows = db.execute(
        select(table.exchange_key, table.topic, table.summary, table.notes, table.kept_code).where(
            table.summarizer == summarizer, table.exchange_key.in_(set(keys))
        )
    )
    return {
        key: ExchangeSummary(topic, summary, notes, code_from_json(code)) for key, topic, summary, notes, code in rows
    }


def record_summary(db: Connection, key: str, summarizer: str, summary: ExchangeSummary, now: datetime) -> None:
    values = {
        "topic": summary.topic,
        "summary": summary.summary,
        "notes": summary.notes,
        "kept_code": code_to_json(summary.kept_code),
        "updated_at": now,
    }
    db.execute(
        sqlite_insert(cast(Table, ExchangeSummaryRecord.__table__))
        .values(exchange_key=key, summarizer=summarizer, **values)
        .on_conflict_do_update(index_elements=["exchange_key", "summarizer"], set_=values)
    )


def code_to_json(kept: tuple[KeptCode, ...]) -> str | None:
    if not kept:
        return None
    return json.dumps([{"item_id": k.item_id, "label": k.label, "size": k.size, "text": k.text} for k in kept])


def code_from_json(stored: str | None) -> tuple[KeptCode, ...]:
    if not stored:
        return ()
    return tuple(KeptCode(c["item_id"], c["label"], int(c["size"]), c["text"]) for c in json.loads(stored))
