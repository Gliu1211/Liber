"""Upgrading a database created by an earlier version of Liber (ARCHITECTURE.md §8).

`create_all` only creates missing tables, so a column added in a later session would be missing
from an existing `requests` table and every insert would fail. Until Alembic arrives, startup
adds missing (nullable) columns.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from liber.session.store import RequestRecord, Store

SESSION_1_REQUESTS_TABLE = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY, created_at DATETIME NOT NULL, method VARCHAR NOT NULL, path TEXT NOT NULL,
    model VARCHAR, stream BOOLEAN, request_headers TEXT, request_body TEXT, status_code INTEGER,
    upstream_request_id VARCHAR, response_body TEXT, stop_reason VARCHAR, input_tokens INTEGER,
    output_tokens INTEGER, cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,
    latency_ms FLOAT, duration_ms FLOAT, error TEXT
)
"""


async def test_columns_added_since_session_1_are_created(tmp_path: Path) -> None:
    db_path = tmp_path / "liber.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(SESSION_1_REQUESTS_TABLE)
        connection.execute(
            "INSERT INTO requests (created_at, method, path) VALUES ('2026-11-01 00:00:00', 'POST', '/v1/messages')"
        )

    store = Store(db_path)
    await store.create_tables()
    await store.add_request(
        RequestRecord(created_at=datetime.now(UTC), method="POST", path="/v1/messages", liber_outcome="passthrough")
    )
    rows = await store.list_requests()
    await store.close()

    assert [row.liber_outcome for row in rows] == [None, "passthrough"]  # old row kept, new row has the column
