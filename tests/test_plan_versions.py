"""Versioned plans on the request path (ARCHITECTURE.md §4 edit plans, §6 break points, ADR 0004 A1).

A plan version is made pending, the user asks for it, and the request path applies it at the
next request that ends at a clean turn boundary and that the plan fits. From then on every
request of the session is rendered with it; undo goes back to exactly the previous render.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from liber import pipeline
from liber.model.anthropic import RecentRequests, parse
from liber.model.items import Kind, Operation, OperationKind
from liber.plan import versions
from liber.plan.apply import summarize_operation
from liber.session.store import Store, StoreBusy
from tests.helpers import FIXTURES, FakeUpstream, recorded


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    store = Store(tmp_path / "liber.db")
    await store.create_tables()
    yield store
    await store.close()


def chat(*texts: str) -> dict[str, Any]:
    roles = ["user", "assistant"]
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 100,
        "messages": [{"role": roles[i % 2], "content": text} for i, text in enumerate(texts)],
    }


def body(request: dict[str, Any]) -> bytes:
    return json.dumps(request).encode()


TURNS = ["u0", "a0", "u1", "a1", "u2", "a2", "u3", "a3", "u4"]


def summary_of_first_exchange(request: dict[str, Any]) -> Operation:
    return summarize_operation(parse(request).items, first=0, last=1, block_number=1)


async def start_session(store: Store, recent: RecentRequests) -> str:
    first = await pipeline.prepare(store, body(chat(*TURNS[:5])), recent)
    assert first.session_id is not None
    return first.session_id


# --- Versions -----------------------------------------------------------------------------------


async def test_versions_build_on_each_other_and_never_change(store: Store) -> None:
    session = await start_session(store, RecentRequests())
    summary = summary_of_first_exchange(chat(*TURNS))
    offload = Operation(kind=OperationKind.OFFLOAD, item_ids=(parse(chat(*TURNS)).items[3].id,))

    first = await versions.create(store, session, (summary,))
    second = await versions.create(store, session, (offload,))
    state = await versions.load_state(store, session)

    assert (first.number, second.number, second.parent) == (1, 2, 1)
    assert second.plan.operations == (summary, offload)  # the newest version plus the new edit
    assert state.pending == second and not state.apply_requested and state.applied == versions.EMPTY
    stored_first = await store.run(lambda db: versions.read_version(db.connection(), session, 1))
    assert stored_first == first  # version 1 is as it was


async def test_undo_with_nothing_applied_is_refused(store: Store) -> None:
    session = await start_session(store, RecentRequests())

    with pytest.raises(versions.PlanError):
        await versions.undo(store, session)
    with pytest.raises(versions.PlanError):
        await versions.apply(store, session)  # nothing pending either


# --- On the request path --------------------------------------------------------------------------


async def test_a_requested_plan_is_applied_at_the_next_clean_turn_and_sent(store: Store) -> None:
    recent = RecentRequests()
    session = await start_session(store, recent)
    await versions.create(store, session, (summary_of_first_exchange(chat(*TURNS)),))
    await versions.apply(store, session)

    prepared = await pipeline.prepare(store, body(chat(*TURNS[:7])), recent)

    assert prepared.outcome == "rendered" and prepared.plan_version == 1
    sent = json.loads(prepared.body)
    assert sent["messages"][0]["content"][0]["text"].startswith('<liber:summary block="1"')
    assert len(sent["messages"]) == 5
    assert (await versions.load_state(store, session)).applied.number == 1


async def test_a_plan_is_not_applied_while_a_tool_loop_is_in_flight(store: Store) -> None:
    # ADR 0004 A1: the request ends with tool results, so the model is mid-task; the plan waits.
    recent = RecentRequests()
    loop = chat(*TURNS[:6])
    loop["messages"][5]["content"] = [{"type": "tool_use", "id": "t1", "name": "ls", "input": {}}]
    loop["messages"].append(
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}
    )
    first = await pipeline.prepare(store, body(chat(*TURNS[:5])), recent)
    assert first.session_id is not None
    await versions.create(store, first.session_id, (summary_of_first_exchange(chat(*TURNS)),))
    await versions.apply(store, first.session_id)

    in_flight = await pipeline.prepare(store, body(loop), recent)
    state = await versions.load_state(store, first.session_id)

    assert in_flight.outcome == "passthrough" and in_flight.plan_version is None
    assert state.pending is not None and state.pending.number == 1 and state.applied.number == 0

    finished = loop["messages"] + [
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "next question"},
    ]
    clean = await pipeline.prepare(store, body({**loop, "messages": finished}), recent)
    assert clean.outcome == "rendered" and clean.plan_version == 1


async def test_undo_restores_the_previous_render_exactly(store: Store) -> None:
    recent = RecentRequests()
    session = await start_session(store, recent)
    final = chat(*TURNS)
    first_summary = summary_of_first_exchange(final)
    second_summary = summarize_operation(parse(final).items, first=2, last=3, block_number=2)
    await versions.create(store, session, (first_summary,))
    await versions.apply(store, session)
    with_v1 = await pipeline.prepare(store, body(final), recent)
    await versions.create(store, session, (second_summary,))
    await versions.apply(store, session)
    with_v2 = await pipeline.prepare(store, body(final), recent)

    await versions.undo(store, session)
    after_undo = await pipeline.prepare(store, body(final), recent)

    assert (with_v1.plan_version, with_v2.plan_version, after_undo.plan_version) == (1, 2, 1)
    assert with_v2.body != with_v1.body
    assert after_undo.body == with_v1.body


async def test_edits_are_held_back_when_the_request_has_thinking(store: Store) -> None:
    # Until Session 4b sends the preserved-thinking opt-in, an edited request with thinking blocks
    # could be rejected by the API, so the client's request goes through and the reason is kept.
    request = json.loads((FIXTURES / "requests" / "long_session_101.json").read_text(encoding="utf-8"))
    recent = RecentRequests()
    first = await pipeline.prepare(store, body(request), recent)
    assert first.session_id is not None
    tool_result = next(i for i in parse(request).items if i.kind is Kind.TOOL_RESULT)
    await versions.create(store, first.session_id, (Operation(kind=OperationKind.OFFLOAD, item_ids=(tool_result.id,)),))
    await versions.apply(store, first.session_id)
    raw = body(request)

    held = await pipeline.prepare(store, raw, recent)

    assert held.outcome == "passthrough" and held.body is raw
    assert held.edit_skipped == pipeline.HELD_FOR_THINKING
    assert held.plan_version == 1 and held.violations == []
    assert [item.id for item in held.offloaded] == [tool_result.id]  # its original is still stored


async def test_a_plan_that_does_not_fit_the_request_is_skipped(store: Store) -> None:
    # The user went back and edited message 2: this branch doesn't have the items the plan is about.
    recent = RecentRequests()
    session = await start_session(store, recent)
    offload_a1 = Operation(kind=OperationKind.OFFLOAD, item_ids=(parse(chat(*TURNS)).items[3].id,))
    await versions.create(store, session, (offload_a1,))
    await versions.apply(store, session)
    await pipeline.prepare(store, body(chat(*TURNS[:7])), recent)  # applied here

    fork = await pipeline.prepare(store, body(chat("u0", "a0", "u1 edited")), recent)

    assert fork.outcome == "passthrough" and fork.plan_version is None
    assert fork.edit_skipped is not None and "doesn't have" in fork.edit_skipped


async def test_plan_outcomes_are_recorded(app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    store: Store = app.state.store
    headers = {"content-type": "application/json"}
    await client.post("/v1/messages", content=body(chat(*TURNS[:5])), headers=headers)
    [first] = await recorded(app)
    assert first.session_id is not None
    await versions.create(store, first.session_id, (summary_of_first_exchange(chat(*TURNS)),))
    await versions.apply(store, first.session_id)

    await client.post("/v1/messages", content=body(chat(*TURNS[:7])), headers=headers)
    rows = await recorded(app)

    assert (rows[-1].liber_outcome, rows[-1].plan_version, rows[-1].edit_skipped) == ("rendered", 1, None)
    assert json.loads(upstream.last_request.content)["messages"][0]["content"][0]["text"].startswith("<liber:summary")


# --- Review findings ----------------------------------------------------------------------------


async def test_undo_back_to_no_plan_takes_effect_and_stays(store: Store) -> None:
    recent = RecentRequests()
    session = await start_session(store, recent)
    await versions.create(store, session, (summary_of_first_exchange(chat(*TURNS)),))
    await versions.apply(store, session)
    await pipeline.prepare(store, body(chat(*TURNS[:7])), recent)  # v1 applied

    await versions.undo(store, session)
    clean_turn = await pipeline.prepare(store, body(chat(*TURNS[:7])), recent)
    later = await pipeline.prepare(store, body(chat(*TURNS)), recent)
    state = await versions.load_state(store, session)

    assert (state.applied.number, state.pending) == (0, None)
    assert (clean_turn.outcome, later.outcome) == ("passthrough", "passthrough")
    assert later.plan_version is None  # no flip back to version 1


async def test_a_pending_version_that_breaks_the_request_gives_way_to_the_applied_one(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    recent = RecentRequests()
    session = await start_session(store, recent)
    final = chat(*TURNS)
    await versions.create(store, session, (summary_of_first_exchange(final),))
    await versions.apply(store, session)
    await pipeline.prepare(store, body(chat(*TURNS[:7])), recent)  # v1 applied
    await versions.create(store, session, (summarize_operation(parse(final).items, 2, 3, block_number=2),))
    await versions.apply(store, session)
    real_render = pipeline.render

    def render_breaking_version_2(parsed: Any, plan: Any) -> dict[str, Any]:
        rendered = real_render(parsed, plan)
        if len(plan.operations) == 2:  # version 2 renders a request the API would reject
            rendered["messages"] = [{"role": "assistant", "content": "oops"}, *rendered["messages"]]
        return rendered

    monkeypatch.setattr(pipeline, "render", render_breaking_version_2)

    prepared = await pipeline.prepare(store, body(final), recent)
    state = await versions.load_state(store, session)

    assert (prepared.outcome, prepared.plan_version) == ("rendered", 1)
    assert prepared.edit_skipped is not None and "version 2 not used" in prepared.edit_skipped
    assert state.applied.number == 1 and state.pending is not None and state.pending.number == 2


async def test_a_held_back_edit_is_not_marked_applied(store: Store) -> None:
    request = json.loads((FIXTURES / "requests" / "long_session_101.json").read_text(encoding="utf-8"))
    recent = RecentRequests()
    first = await pipeline.prepare(store, body(request), recent)
    assert first.session_id is not None
    tool_result = next(i for i in parse(request).items if i.kind is Kind.TOOL_RESULT)
    await versions.create(store, first.session_id, (Operation(kind=OperationKind.OFFLOAD, item_ids=(tool_result.id,)),))
    await versions.apply(store, first.session_id)

    await pipeline.prepare(store, body(request), recent)
    state = await versions.load_state(store, first.session_id)

    assert state.applied.number == 0  # never sent, so never in effect
    assert state.pending is not None and state.pending.number == 1 and state.apply_requested


async def test_if_applying_a_version_cannot_be_recorded_the_original_is_sent(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    recent = RecentRequests()
    session = await start_session(store, recent)
    await versions.create(store, session, (summary_of_first_exchange(chat(*TURNS)),))
    await versions.apply(store, session)

    async def locked(*args: Any) -> None:
        raise StoreBusy("the database is locked by another writer")

    monkeypatch.setattr(versions, "promote_pending", locked)
    raw = body(chat(*TURNS[:7]))

    prepared = await pipeline.prepare(store, raw, recent)

    assert prepared.outcome == "fallback" and prepared.body is raw
    assert prepared.error is not None and "StoreBusy" in prepared.error


async def test_a_plan_that_crashes_gives_way_to_the_next_version(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    recent = RecentRequests()
    session = await start_session(store, recent)
    await versions.create(store, session, (summary_of_first_exchange(chat(*TURNS)),))
    await versions.apply(store, session)
    real_render = pipeline.render

    def crashing_with_plans(parsed: Any, plan: Any) -> dict[str, Any]:
        if not plan.is_empty:
            raise RuntimeError("a bug in applying plans")
        return real_render(parsed, plan)

    monkeypatch.setattr(pipeline, "render", crashing_with_plans)

    prepared = await pipeline.prepare(store, body(chat(*TURNS[:7])), recent)

    assert prepared.outcome == "passthrough" and prepared.plan_version is None
    assert prepared.edit_skipped is not None and "RuntimeError" in prepared.edit_skipped


async def test_a_damaged_plan_row_means_no_plan_not_a_broken_session(store: Store) -> None:
    recent = RecentRequests()
    session = await start_session(store, recent)

    def damage(db: Any) -> None:  # the applied version points at a row that doesn't exist
        db.connection().exec_driver_sql(
            "INSERT INTO plans (session_id, applied_version, pending_version, apply_requested, updated_at) "
            "VALUES (?, 7, NULL, 0, '2026-11-04')",
            (session,),
        )

    await store.run(damage)

    prepared = await pipeline.prepare(store, body(chat(*TURNS[:7])), recent)

    assert prepared.outcome == "passthrough" and prepared.session_id == session
    assert prepared.new_messages == 2  # the conversation was still registered
