"""The real model client on the SDK (worker/client.py), checked offline on a fake transport (hard rule 7)."""

import json
from typing import Any

import httpx2
import pytest
from anthropic import BadRequestError

from liber.cache import exchanges
from liber.model.anthropic import parse
from liber.plan import cost
from liber.worker.client import AnthropicModelClient, ModelReplyRejected, ModelUnavailable, WorkerMisconfigured
from liber.worker.summarize import SUMMARY_SCHEMA
from liber.worker.tokens import stub_tool

KEY = "sk-ant-WORKER-test-key"


def client_answering(status: int, body: dict[str, Any], seen: list[httpx2.Request]) -> AnthropicModelClient:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json=body)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return AnthropicModelClient(KEY, "https://upstream.test", http_client=http)


def message(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


async def test_structured_asks_for_json_with_the_schema_and_no_thinking() -> None:
    seen: list[httpx2.Request] = []
    client = client_answering(200, message('{"topic": "t", "summary": "s"}'), seen)

    reply = await client.structured(
        "claude-haiku-5-5", "the instructions", "<exchange>[]</exchange>", SUMMARY_SCHEMA, 512
    )

    [request] = seen
    sent = json.loads(request.content)
    assert reply == {"topic": "t", "summary": "s"}
    assert request.url == "https://upstream.test/v1/messages"
    assert KEY in request.headers.get("authorization", "") + request.headers.get("x-api-key", "")  # its own key
    assert sent["system"] == "the instructions"
    assert sent["messages"] == [{"role": "user", "content": "<exchange>[]</exchange>"}]
    assert sent["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}}
    assert sent["thinking"] == {"type": "disabled"} and sent["max_tokens"] == 512


async def test_count_tokens_uses_the_counting_endpoint() -> None:
    seen: list[httpx2.Request] = []
    client = client_answering(200, {"input_tokens": 42}, seen)

    tokens = await client.count_tokens("claude-opus-5-5", [{"role": "user", "content": "."}], [])
    with_tools = await client.count_tokens("claude-opus-5-5", [{"role": "user", "content": "."}], [stub_tool("bash")])

    assert tokens == with_tools == 42
    assert seen[0].url.path == "/v1/messages/count_tokens"
    assert json.loads(seen[0].content)["model"] == "claude-opus-5-5" and "tools" not in json.loads(seen[0].content)
    assert json.loads(seen[1].content)["tools"] == [stub_tool("bash")]


@pytest.mark.parametrize("status", [408, 429, 500, 529])
async def test_rate_limits_and_server_errors_are_worth_retrying(status: int) -> None:
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    client = client_answering(status, error, [])

    with pytest.raises(ModelUnavailable):
        await client.count_tokens("claude-opus-5-5", [{"role": "user", "content": "."}], [])


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_a_wrong_key_or_model_means_no_call_can_succeed(status: int) -> None:
    error = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    client = client_answering(status, error, [])

    with pytest.raises(WorkerMisconfigured) as raised:
        await client.count_tokens("claude-opus-5-5", [{"role": "user", "content": "."}], [])
    assert str(raised.value) == f"HTTP {status}"  # only the status: the SDK's message quotes the response


async def test_a_bad_request_is_not_retried() -> None:
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
    client = client_answering(400, error, [])

    with pytest.raises(BadRequestError):
        await client.structured("claude-haiku-5-5", "s", "p", SUMMARY_SCHEMA, 512)


@pytest.mark.parametrize(
    "reply", [message("", stop_reason="refusal"), message('{"topic"', stop_reason="max_tokens"), message("not json")]
)
async def test_a_reply_liber_cant_use_is_rejected(reply: dict[str, Any]) -> None:
    client = client_answering(200, reply, [])

    with pytest.raises(ModelReplyRejected):
        await client.structured("claude-haiku-5-5", "s", "p", SUMMARY_SCHEMA, 512)


def test_exact_counts_replace_the_estimates_of_their_exchange() -> None:
    request = {
        "model": "claude-opus-5-5",
        "messages": [
            {"role": "user", "content": "u0 " * 40},
            {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "text", "text": "a0"}],
            },
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ],
    }
    items = parse(request).items
    found = exchanges.completed_exchanges(items)

    counted = cost.with_exact_counts(items, found, {found[0].key: 1000})

    by_id = {item.id: item.tokens for item in counted}
    first, second = found
    non_thinking = [i for i in first.items if i.kind != "thinking"]
    assert sum(by_id[i.id] for i in non_thinking) == 1000  # the exact count, split by estimate, nothing lost
    assert by_id[non_thinking[0].id] > by_id[non_thinking[1].id]  # the long message gets more of it
    thinking_item = next(i for i in first.items if i.kind == "thinking")
    assert by_id[thinking_item.id] == thinking_item.tokens  # thinking isn't in the count: estimate kept
    assert all(by_id[i.id] == i.tokens for i in second.items)  # no exact count: estimates


def test_a_system_message_inside_an_exchange_keeps_its_estimate() -> None:
    request = {
        "model": "claude-opus-5-5",
        "messages": [
            {"role": "user", "content": "u0"},
            {"role": "system", "content": [{"type": "text", "text": "a mid-conversation system note"}]},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "u1"},
        ],
    }
    items = parse(request).items
    [exchange] = exchanges.completed_exchanges(items)

    counted = {i.id: i.tokens for i in cost.with_exact_counts(items, [exchange], {exchange.key: 7})}

    system = next(i for i in items if i.role == "system")
    assert counted[system.id] == system.tokens
    assert sum(counted[i.id] for i in exchange.items if i.role != "system") == 7
