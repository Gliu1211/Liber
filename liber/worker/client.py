"""The worker's connection to a model: token counts and structured replies (ARCHITECTURE.md §9, D9, D10).

The worker only needs two calls, so it talks to this small interface (`ModelClient`) instead of
the SDK directly; tests pass a fake that never touches the network (hard rule 7).

`AnthropicModelClient` is the real one, on the official SDK:
- `count_tokens` is POST /v1/messages/count_tokens (checked against the docs on 2026-11-06:
  model-specific, stateless, returns `input_tokens`).
- `structured` asks for a JSON reply with structured outputs (`output_config.format`), so the
  reply always has the shape Liber asks for, whatever the conversation text says.

It uses its own key (LIBER_WORKER_API_KEY), never a client's: Liber passes client credentials
through and never keeps them (hard rule 2), and the worker runs when no request is in flight.
The SDK's own retries are off; `with_retries` does it, so the policy is in one visible place.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

import structlog
from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic, DefaultAsyncHttpxClient

log = structlog.get_logger(__name__)

T = TypeVar("T")


class ModelUnavailable(Exception):
    """A failure worth retrying later: rate limited, overloaded, a server error, the network."""


class ModelReplyRejected(Exception):
    """The model answered, but not with something Liber can use (a refusal, cut off, not JSON)."""


class WorkerMisconfigured(Exception):
    """No call can succeed: the key is wrong or lacks permission, or the model doesn't exist."""


class ModelClient(Protocol):
    async def count_tokens(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        """Input tokens of a request with these messages and tool definitions, for `model`'s tokenizer."""
        ...

    async def structured(
        self, model: str, system: str, prompt: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        """The model's reply to `prompt`, as a JSON object following `schema`."""
        ...


class AnthropicModelClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 60.0,
        http_client: DefaultAsyncHttpxClient | None = None,
    ) -> None:
        """`http_client`: tests pass one on a fake transport, so the real request shape is checked offline."""
        self.sdk = AsyncAnthropic(
            api_key=api_key, base_url=base_url, max_retries=0, timeout=timeout_seconds, http_client=http_client
        )

    async def count_tokens(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        try:
            if tools:
                result = await self.sdk.messages.count_tokens(model=model, messages=messages, tools=tools)  # type: ignore[arg-type]
            else:
                result = await self.sdk.messages.count_tokens(model=model, messages=messages)  # type: ignore[arg-type]
        except Exception as exc:
            raise_if_unavailable(exc)
            raise
        return result.input_tokens

    async def aclose(self) -> None:
        await self.sdk.close()

    async def structured(
        self, model: str, system: str, prompt: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        try:
            response = await self.sdk.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                # A summary needs no deliberation: no thinking, low effort (cheapest). On Claude
                # Haiku 5.5, thinking can be off at effort "high" or below.
                thinking={"type": "disabled"},
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            )
        except Exception as exc:
            raise_if_unavailable(exc)
            raise
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ModelReplyRejected(f"stop_reason {response.stop_reason}")
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise ModelReplyRejected("no text in the reply")
        try:
            reply = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelReplyRejected("the reply isn't JSON") from exc
        if not isinstance(reply, dict):
            raise ModelReplyRejected("the reply isn't a JSON object")
        return reply


def raise_if_unavailable(exc: Exception) -> None:
    """Sort an SDK error: worth retrying (what the SDK itself would retry: connection errors, 408,
    409, 429, 5xx), or a sign that no call will work (401, 403, 404). Anything else is raised as is.

    Only the status code goes into the new error: the SDK's own message quotes the response.
    """
    if isinstance(exc, APIConnectionError):  # includes timeouts
        raise ModelUnavailable(type(exc).__name__) from exc
    if isinstance(exc, APIStatusError):
        if exc.status_code in (408, 409, 429) or exc.status_code >= 500:
            raise ModelUnavailable(f"HTTP {exc.status_code}") from exc
        if exc.status_code in (401, 403, 404):
            raise WorkerMisconfigured(f"HTTP {exc.status_code}") from exc


async def with_retries(
    call: Callable[[], Awaitable[T]],
    attempts: int,
    first_delay_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """`call()`, tried again after 1x, 2x, 4x... `first_delay_seconds` while the model is unavailable.

    Anything else (a bad request, a rejected reply) is raised at once: trying again wouldn't help.
    """
    delay = first_delay_seconds
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except ModelUnavailable as exc:
            if attempt == attempts:
                raise
            log.info("worker_retry", attempt=attempt, delay_seconds=delay, reason=str(exc))
            await sleep(delay)
            delay *= 2
    raise AssertionError("unreachable: attempts must be at least 1")
