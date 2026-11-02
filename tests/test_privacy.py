"""Credentials pass through but are never stored or logged (CLAUDE.md hard rule 2, D12, §11)."""

import logging
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from liber.config import Settings
from liber.log import REDACTED, redact_credentials
from liber.session.store import RequestRecord
from tests.helpers import (
    MESSAGES_BODY,
    SECRET_API_KEY,
    SECRET_BEARER,
    STREAM_FIXTURE,
    FakeUpstream,
    recorded,
    sse_response,
)

SECRETS = [SECRET_API_KEY, SECRET_BEARER, SECRET_BEARER.removeprefix("Bearer ")]


def database_bytes(data_dir: Path) -> bytes:
    """Raw bytes of every SQLite file (main db plus any journal/WAL), to catch any stray copy."""
    return b"".join(path.read_bytes() for path in sorted(data_dir.glob("liber.db*")))


async def test_credentials_are_forwarded_but_never_stored_or_logged(
    app: FastAPI,
    client: httpx.AsyncClient,
    upstream: FakeUpstream,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)  # the most verbose level any logger could use
    responses = iter(
        [
            sse_response(STREAM_FIXTURE.read_bytes()),
            httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error"}}),
            httpx.Response(200, json={"data": []}),
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    upstream.handler = handler
    secret_headers = {
        "x-api-key": SECRET_API_KEY,
        "authorization": SECRET_BEARER,
        "anthropic-version": "2023-06-01",
    }
    await client.post("/v1/messages", json=MESSAGES_BODY, headers=secret_headers)
    await client.post("/v1/messages", json=MESSAGES_BODY, headers=secret_headers)
    await client.get("/v1/models", headers=secret_headers)
    rows = await recorded(app)

    # Forwarded untouched (D12) ...
    for sent in upstream.requests:
        assert sent.headers["x-api-key"] == SECRET_API_KEY
        assert sent.headers["authorization"] == SECRET_BEARER

    # ... but never written to the database ...
    assert len(rows) == 2
    for row in rows:
        for column in RequestRecord.__table__.columns:
            value = str(getattr(row, column.key))
            assert not any(secret in value for secret in SECRETS), column.key
    stored = database_bytes(settings.data_dir)
    assert stored, "expected a database file"
    assert not any(secret.encode() in stored for secret in SECRETS)

    # ... and never logged, by Liber or by any library it uses.
    assert caplog.records, "expected some log output to check"
    for record in caplog.records:
        line = record.getMessage()
        assert not any(secret in line for secret in SECRETS), line


def test_redaction_processor_masks_credential_fields() -> None:
    event = {
        "event": "debug",
        "x-api-key": SECRET_API_KEY,
        "Authorization": SECRET_BEARER,
        "headers": {"x-api-key": SECRET_API_KEY, "anthropic-version": "2023-06-01"},
        "path": "/v1/messages",
    }

    cleaned = redact_credentials(None, "info", event)

    assert cleaned["x-api-key"] == REDACTED
    assert cleaned["Authorization"] == REDACTED
    assert cleaned["headers"] == {"x-api-key": REDACTED, "anthropic-version": "2023-06-01"}
    assert cleaned["path"] == "/v1/messages"
