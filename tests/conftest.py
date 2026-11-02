"""Pytest fixtures: an isolated Liber app wired to a fake upstream, and a client for it."""

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from liber.app import create_app
from liber.config import Settings
from liber.session.store import RequestRecord
from tests.helpers import FakeUpstream, recorded


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Every test gets its own data directory, so databases never leak between tests.
    return Settings(data_dir=tmp_path / "liber-data", upstream_base_url="https://upstream.test")


@pytest.fixture
async def app(settings: Settings, upstream: FakeUpstream) -> AsyncIterator[FastAPI]:
    app = create_app(settings, upstream_transport=upstream.transport)
    # httpx's ASGITransport does not run lifespan events, so run startup/shutdown here.
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://liber.test") as client:
        yield client


@pytest.fixture
def records(app: FastAPI) -> Callable[[], Awaitable[list[RequestRecord]]]:
    """Call `await records()` to read what the proxy stored."""

    async def read() -> list[RequestRecord]:
        return await recorded(app)

    return read
