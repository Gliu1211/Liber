"""Exact token counts per exchange, from the token-counting endpoint (ARCHITECTURE.md D9, §9).

Why per exchange and not per block (docs/decisions/0013): the endpoint counts a whole request,
and a block alone often isn't a valid one (a tool result needs its tool call, a thinking block
its assistant turn). A completed exchange is: its tool calls and results are all inside it.

    count(exchange's messages + a one-word user turn) - count(the one-word user turn alone)

The user turn at the end keeps the request ending on a user message (several current models
reject an assistant message last). Thinking blocks are left out of the count: Liber leaves them
out of every request once a plan takes effect (ADR 0012), so their estimate is all that's needed.
An exchange with tool calls is counted with a stub definition of each tool it calls (the Messages
API rejects tool blocks without `tools`), and the baseline is counted with the same stubs, so they
cancel out. Counts depend on the tokenizer, so they're made with the session's own model.
"""

from typing import Any

from liber.cache.exchanges import Exchange
from liber.model.items import Item, Kind, Role
from liber.worker.client import ModelClient

END_TURN = {"role": "user", "content": "."}


class TokenCounter:
    def __init__(self, client: ModelClient) -> None:
        self.client = client
        # What END_TURN alone counts, per model and set of stub tools.
        self.baselines: dict[tuple[str, tuple[str, ...]], int] = {}

    async def count(self, model: str, exchange: Exchange[Item]) -> int:
        """The exchange's tokens without its thinking blocks (or system messages), for `model`."""
        names = tool_names(exchange)
        tools = [stub_tool(name) for name in names]
        if (model, names) not in self.baselines:
            self.baselines[(model, names)] = await self.client.count_tokens(model, [END_TURN], tools)
        total = await self.client.count_tokens(model, [*counted_messages(exchange), END_TURN], tools)
        return max(total - self.baselines[(model, names)], 0)


def counted_messages(exchange: Exchange[Item]) -> list[dict[str, Any]]:
    """The exchange as messages, as the client sent them, without thinking blocks.

    A mid-conversation system message has no place in a count of the exchange's content; it keeps
    its estimate. Prompt-cache markers are left out: they don't change a count, and the API limits
    how many a request may have.
    """
    messages: list[dict[str, Any]] = []
    current: int | None = None
    for item in exchange.items:
        if item.kind is Kind.THINKING or item.role is Role.SYSTEM:
            continue
        if item.msg_index != current:
            messages.append({"role": str(item.role), "content": []})
            current = item.msg_index
        messages[-1]["content"].append({key: value for key, value in item.block.items() if key != "cache_control"})
    return messages


def tool_names(exchange: Exchange[Item]) -> tuple[str, ...]:
    """The names of the tools the exchange calls, sorted (part of the baseline's key)."""
    names = {item.block.get("name") for item in exchange.items if item.kind is Kind.TOOL_USE}
    return tuple(sorted(name for name in names if isinstance(name, str)))


def stub_tool(name: str) -> dict[str, Any]:
    return {"name": name, "description": ".", "input_schema": {"type": "object"}}
