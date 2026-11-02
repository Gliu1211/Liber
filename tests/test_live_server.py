"""Timing tests that need a real HTTP server (the in-process test client buffers responses).

These run uvicorn on a free 127.0.0.1 port; the upstream is still the in-process fake, so
nothing leaves the machine.
"""

import asyncio
from collections.abc import AsyncIterator

import httpx

from liber.app import create_app
from liber.config import Settings
from tests.helpers import MESSAGES_BODY, FakeUpstream, recorded, running_server

FIRST_EVENT = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{}}}\n\n'
LAST_EVENT = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def local_client(base_url: str) -> httpx.AsyncClient:
    # trust_env=False: never route test traffic through a proxy configured in the environment.
    return httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=10)


async def test_events_reach_the_client_before_the_upstream_finishes(settings: Settings, upstream: FakeUpstream) -> None:
    client_saw_first_event = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield FIRST_EVENT
        # The upstream refuses to continue until the client has received the first event.
        # If Liber buffered the response, this would wait forever and time out.
        await asyncio.wait_for(client_saw_first_event.wait(), timeout=5)
        yield LAST_EVENT

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())

    upstream.handler = handler
    app = create_app(settings, upstream_transport=upstream.transport)

    async with running_server(app) as base_url, local_client(base_url) as client:
        received = b""
        async with client.stream("POST", "/v1/messages", json=MESSAGES_BODY) as response:
            assert response.status_code == 200
            async for chunk in response.aiter_raw():
                received += chunk
                if received.startswith(FIRST_EVENT):
                    client_saw_first_event.set()

    assert received == FIRST_EVENT + LAST_EVENT


async def test_client_disconnect_releases_the_upstream_and_is_recorded(
    settings: Settings, upstream: FakeUpstream
) -> None:
    upstream_finished = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        try:
            yield FIRST_EVENT
            await asyncio.sleep(30)  # a long silence; the client gives up meanwhile
            yield LAST_EVENT
        finally:
            upstream_finished.set()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())

    upstream.handler = handler
    app = create_app(settings, upstream_transport=upstream.transport)

    async with running_server(app) as base_url:
        async with (
            local_client(base_url) as client,
            client.stream("POST", "/v1/messages", json=MESSAGES_BODY) as response,
        ):
            async for chunk in response.aiter_raw():
                assert chunk.startswith(b"event: message_start")
                break  # hang up after the first event

        # Liber must notice, stop reading from the upstream, and record what happened.
        await asyncio.wait_for(upstream_finished.wait(), timeout=5)
        [row] = await recorded(app)

    assert row.status_code == 200
    assert row.error is not None and "disconnect" in row.error
