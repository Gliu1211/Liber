"""`liber plan show|create|apply|undo` (plan/commands.py, liber/cli.py)."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from liber import cli, pipeline
from liber.config import Settings
from liber.model.anthropic import RecentRequests
from liber.plan import commands, versions
from liber.plan.versions import PlanError
from liber.session.store import Store

TURNS = ["u0", "a0", "u1", "a1", "u2", "a2", "u3"]


def chat(*texts: str) -> bytes:
    roles = ["user", "assistant"]
    request: dict[str, Any] = {
        "model": "claude-opus-5-5",
        "max_tokens": 100,
        "messages": [{"role": roles[i % 2], "content": text} for i, text in enumerate(texts)],
    }
    return json.dumps(request).encode()


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    store = Store(tmp_path / "liber.db")
    await store.create_tables()
    yield store
    await store.close()


async def a_session(store: Store) -> str:
    prepared = await pipeline.prepare(store, chat(*TURNS))
    assert prepared.session_id is not None
    return prepared.session_id


async def test_show_lists_sessions_then_a_sessions_items(store: Store) -> None:
    session = await a_session(store)

    listing = await commands.show(store)
    detail = await commands.show(store, session)

    assert session in listing
    assert "applied plan version: 0" in detail
    assert len([line for line in detail.splitlines() if line.strip().startswith(("0 ", "6 "))]) == 2  # messages 0 and 6


async def test_create_apply_and_undo_from_the_command_line(store: Store) -> None:
    session = await a_session(store)

    created = await commands.create(store, session, summarize_through=1)
    applied = await commands.apply(store, session)
    used = await pipeline.prepare(store, chat(*TURNS), RecentRequests())
    second = await commands.create(store, session, summarize_through=3)  # continues after message 1
    state = await versions.load_state(store, session)

    assert "version 1" in created and "version 1" in applied
    assert used.outcome == "rendered" and used.plan_version == 1
    assert "version 2" in second
    assert state.pending is not None
    [_, next_summary] = state.pending.plan.operations
    assert 'covers="msgs 2-3"' in (next_summary.summary or "") and 'block="2"' in (next_summary.summary or "")
    assert "version 0" in await commands.undo(store, session)


async def test_create_refuses_what_the_request_path_would_refuse(store: Store) -> None:
    session = await a_session(store)

    with pytest.raises(PlanError, match="starts an exchange"):
        await commands.create(store, session, summarize_through=2)  # message 3 is the assistant's
    with pytest.raises(PlanError, match="no such item"):
        await commands.create(store, session, offload=("not-an-item",))
    with pytest.raises(PlanError, match="nothing to do"):
        await commands.create(store, session)
    with pytest.raises(PlanError, match="no session"):
        await commands.create(store, "missing", summarize_through=1)


def test_the_cli_runs_plan_commands_against_the_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LIBER_DATA_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as no_database:
        cli.main(["plan", "show"])
    assert no_database.value.code == 1 and "No Liber database" in capsys.readouterr().out

    async def seed() -> None:
        store = Store(Settings().db_path)
        await store.create_tables()
        await pipeline.prepare(store, chat(*TURNS))
        await store.close()

    asyncio.run(seed())
    with pytest.raises(SystemExit) as shown:
        cli.main(["plan", "show"])
    with pytest.raises(SystemExit) as refused:
        cli.main(["plan", "undo", "--session", "missing"])

    assert shown.value.code == 0
    assert refused.value.code == 1 and "liber plan:" in capsys.readouterr().out
