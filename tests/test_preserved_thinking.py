"""Session 4b: edited requests keep their thinking valid (ADR 0004, docs/decisions/0012), break points (§6).

On current models a thinking block is valid only after the exact history that produced it. When
a plan version takes effect, every thinking block in the history becomes stale: Liber leaves
those out (a leading run, so later blocks stay valid), adds the `drop_block` opt-in as a safety
net, and merges its beta into the client's header. Later thinking, produced against Liber's
rendering, is kept.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from liber import pipeline
from liber.config import DEFAULT_PRICING
from liber.model.anthropic import RecentRequests, parse, render
from liber.model.items import Kind, Operation, OperationKind, Plan
from liber.plan import commands, cost, versions
from liber.plan.apply import PlanNotApplicable, summarize_operation
from liber.plan.policy import PlanPolicy, context_share, is_minor
from liber.plan.thinking import BINDING_BETA
from liber.proxy.forward import with_betas
from liber.proxy.sse import ReplyCapture
from liber.session.chain import seconds_between
from liber.session.store import Store
from tests.helpers import FakeUpstream, recorded, sse_response


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    store = Store(tmp_path / "liber.db")
    await store.create_tables()
    yield store
    await store.close()


def thinking(n: int) -> dict[str, Any]:
    return {"type": "thinking", "thinking": f"reasoning {n}", "signature": f"sig-{n}"}


def answer(n: int) -> dict[str, Any]:
    return {"role": "assistant", "content": [thinking(n), {"type": "text", "text": f"answer {n}"}]}


def conversation(turns: int, **request: Any) -> dict[str, Any]:
    """u0, a0, u1, a1, ... u<turns>: every answer carries a thinking block, like Claude Code's."""
    messages: list[dict[str, Any]] = []
    for n in range(turns):
        messages += [{"role": "user", "content": f"question {n}"}, answer(n)]
    messages.append({"role": "user", "content": f"question {turns}"})
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 1000,
        "thinking": {"type": "adaptive"},
        **request,
        "messages": messages,
    }


def body(request: dict[str, Any]) -> bytes:
    return json.dumps(request).encode()


def thinking_texts(request: dict[str, Any]) -> list[str]:
    return [
        block["thinking"]
        for message in request["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "thinking"
    ]


async def with_summary_requested(store: Store, recent: RecentRequests, final: dict[str, Any]) -> str:
    """A session that has seen `final` minus its last turn, with a summary of exchange 0 requested."""
    first = await pipeline.prepare(store, body({**final, "messages": final["messages"][:3]}), recent)
    assert first.session_id is not None
    summary = summarize_operation(parse(final).items, first=0, last=1, block_number=1)
    await versions.create(store, first.session_id, (summary,))
    await versions.apply(store, first.session_id)
    return first.session_id


# --- Edited requests and their thinking ---------------------------------------------------------


async def test_when_a_version_takes_effect_all_earlier_thinking_is_left_out_and_the_opt_in_added(
    store: Store,
) -> None:
    recent = RecentRequests()
    request = conversation(2)  # u0 a0 u1 a1 u2
    session = await with_summary_requested(store, recent, request)

    prepared = await pipeline.prepare(store, body(request), recent)
    sent = json.loads(prepared.body)
    state = await versions.load_state(store, session)

    assert prepared.outcome == "rendered" and prepared.plan_version == 1
    assert thinking_texts(sent) == []  # reasoning 1 was produced against the unedited history
    assert sent["messages"][1]["content"] == [{"type": "text", "text": "answer 1"}]  # its text stays
    assert sent["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert prepared.betas == (BINDING_BETA,)
    assert (state.applied.number, state.activated_at) == (1, 5)


async def test_thinking_produced_after_the_version_took_effect_is_kept(store: Store) -> None:
    recent = RecentRequests()
    request = conversation(2)
    await with_summary_requested(store, recent, request)
    await pipeline.prepare(store, body(request), recent)  # version 1 takes effect at 5 messages

    later = conversation(3)  # the model answered (reasoning 2, against Liber's rendering) and u3 came
    prepared = await pipeline.prepare(store, body(later), recent)

    assert prepared.outcome == "rendered"
    assert thinking_texts(json.loads(prepared.body)) == ["reasoning 2"]  # the tail of the client's thinking
    assert prepared.violations == []


async def test_a_client_that_asked_for_errors_gets_its_own_request(store: Store) -> None:
    recent = RecentRequests()
    binding = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}}
    request = conversation(2, thinking=binding)
    session = await with_summary_requested(store, recent, request)
    raw = body(request)

    prepared = await pipeline.prepare(store, raw, recent)

    assert prepared.outcome == "passthrough" and prepared.body is raw and prepared.betas == ()
    assert prepared.edit_skipped is not None and '"error"' in prepared.edit_skipped
    assert (await versions.load_state(store, session)).applied.number == 0  # not sent, so not in effect


async def test_a_client_that_already_drops_keeps_its_setting(store: Store) -> None:
    recent = RecentRequests()
    binding = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    request = conversation(2, thinking=binding)
    await with_summary_requested(store, recent, request)

    prepared = await pipeline.prepare(store, body(request), recent)

    assert prepared.outcome == "rendered" and prepared.betas == (BINDING_BETA,)
    assert json.loads(prepared.body)["thinking"] == binding


async def test_thinking_left_out_entirely_is_made_explicit_where_that_changes_nothing(store: Store) -> None:
    recent = RecentRequests()
    request = conversation(2)
    del request["thinking"]  # on Claude Opus 5.5 that means adaptive thinking
    await with_summary_requested(store, recent, request)

    sent = json.loads((await pipeline.prepare(store, body(request), recent)).body)

    assert sent["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}


async def test_an_unknown_model_with_thinking_gets_its_own_request(store: Store) -> None:
    recent = RecentRequests()
    request = conversation(3, model="some-future-model")
    await with_summary_requested(store, recent, request)
    await pipeline.prepare(store, body(conversation(2, model="some-future-model")), recent)  # v1 takes effect
    raw = body(request)  # reasoning 2 was produced after: it stays, and this model isn't confirmed

    prepared = await pipeline.prepare(store, raw, recent)

    assert prepared.outcome == "passthrough" and prepared.body is raw
    assert prepared.edit_skipped is not None and "isn't confirmed" in prepared.edit_skipped


async def test_between_tools_thinking_takes_no_opt_in_and_relies_on_leaving_stale_thinking_out(
    store: Store,
) -> None:
    recent = RecentRequests()
    request = conversation(2, model="claude-sonnet-5-5", thinking={"type": "between_tools"})
    await with_summary_requested(store, recent, request)

    prepared = await pipeline.prepare(store, body(request), recent)

    assert prepared.outcome == "rendered" and prepared.betas == ()
    assert json.loads(prepared.body)["thinking"] == {"type": "between_tools"}  # no other field allowed
    assert thinking_texts(json.loads(prepared.body)) == []


async def test_after_an_undo_to_no_plan_thinking_from_the_edited_period_is_still_left_out(store: Store) -> None:
    # Reasoning produced while version 1 was in effect was produced against Liber's rendering;
    # sent after the client's own history, it would be rejected.
    recent = RecentRequests()
    session = await with_summary_requested(store, recent, conversation(2))
    await pipeline.prepare(store, body(conversation(2)), recent)  # v1 at 5 messages
    await pipeline.prepare(store, body(conversation(3)), recent)  # reasoning 2 under v1
    await versions.undo(store, session)

    undone = await pipeline.prepare(store, body(conversation(4)), recent)  # back to no plan at 9
    after = await pipeline.prepare(store, body(conversation(5)), recent)

    assert undone.plan_version is None and undone.outcome == "rendered"
    assert thinking_texts(json.loads(undone.body)) == []
    assert thinking_texts(json.loads(after.body)) == ["reasoning 4"]  # produced after the undo


async def test_a_conversation_that_never_had_a_plan_is_sent_byte_for_byte(store: Store) -> None:
    raw = body(conversation(3))

    prepared = await pipeline.prepare(store, raw, RecentRequests())

    assert prepared.outcome == "passthrough" and prepared.body is raw and prepared.betas == ()


# --- The beta header -------------------------------------------------------------------------------


def test_betas_are_merged_into_the_clients_header() -> None:
    assert with_betas([(b"x-api-key", b"k")], [BINDING_BETA]) == [
        (b"x-api-key", b"k"),
        (b"anthropic-beta", BINDING_BETA.encode()),
    ]
    assert with_betas([(b"anthropic-beta", b"other-2026-01-01")], [BINDING_BETA]) == [
        (b"anthropic-beta", b"other-2026-01-01," + BINDING_BETA.encode())
    ]
    already = [(b"anthropic-beta", b"other, " + BINDING_BETA.encode())]
    assert with_betas(list(already), [BINDING_BETA]) == already


async def test_the_upstream_gets_the_merged_header_and_the_response_is_recorded(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    store: Store = app.state.store
    headers = {"content-type": "application/json", "anthropic-beta": "other-2026-01-01"}
    request = conversation(2)
    await client.post("/v1/messages", content=body({**request, "messages": request["messages"][:3]}), headers=headers)
    [first] = await recorded(app)
    assert first.session_id is not None
    await versions.create(store, first.session_id, (summarize_operation(parse(request).items, 0, 1, 1),))
    await versions.apply(store, first.session_id)
    dropped = [{"type": "thinking_dropped", "path": "messages.1.content.0", "reason": "prefix_binding_mismatch"}]
    message_start = {
        "type": "message_start",
        "message": {"type": "message", "content": [], "usage": {"input_tokens": 9}, "input_transformations": dropped},
    }
    events = [("message_start", message_start), ("message_stop", {"type": "message_stop"})]
    stream = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)

    async def answer_with_stream(_: httpx.Request) -> httpx.Response:
        return sse_response(stream.encode())

    upstream.handler = answer_with_stream
    await client.post("/v1/messages", content=body(request), headers=headers)
    rows = await recorded(app)

    assert upstream.last_request.headers["anthropic-beta"] == f"other-2026-01-01,{BINDING_BETA}"
    assert rows[-1].liber_outcome == "rendered"
    assert rows[-1].input_transformations is not None and json.loads(rows[-1].input_transformations) == dropped
    assert rows[-1].context_share is not None and rows[-1].context_share > 0


def test_input_transformations_are_read_from_json_bodies_and_late_message_deltas() -> None:
    entries = [{"type": "thinking_dropped", "path": "messages.3.content.0", "reason": "model_binding_mismatch"}]
    plain = ReplyCapture("application/json", None)
    plain.feed(json.dumps({"type": "message", "content": [], "usage": {}, "input_transformations": []}).encode())
    stream = ReplyCapture("text/event-stream", None)
    events = [
        {
            "type": "message_start",
            "message": {"type": "message", "content": [], "usage": {}, "input_transformations": []},
        },
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}, "input_transformations": entries},
    ]
    stream.feed("".join(f"data: {json.dumps(event)}\n\n" for event in events).encode())

    assert plain.result().input_transformations == []
    assert stream.result().input_transformations == entries  # the later one wins


# --- Break points (§6) -------------------------------------------------------------------------------


async def test_a_pending_version_takes_effect_on_its_own_after_an_idle_period(store: Store) -> None:
    recent = RecentRequests()
    request = conversation(2)
    first = await pipeline.prepare(store, body({**request, "messages": request["messages"][:3]}), recent)
    assert first.session_id is not None
    await versions.create(
        store, first.session_id, (summarize_operation(parse(request).items, 0, 1, 1),)
    )  # not requested

    busy = await pipeline.prepare(store, body(request), recent)  # default: 5 minutes idle needed
    idle = await pipeline.prepare(store, body(request), recent, PlanPolicy(idle_apply_seconds=0.0))

    assert busy.plan_version is None
    assert idle.plan_version == 1 and idle.outcome == "rendered"


async def test_the_threshold_is_recorded_and_applies_only_when_opted_in(store: Store) -> None:
    recent = RecentRequests()
    request = conversation(2)
    first = await pipeline.prepare(store, body({**request, "messages": request["messages"][:3]}), recent)
    assert first.session_id is not None
    await versions.create(store, first.session_id, (summarize_operation(parse(request).items, 0, 1, 1),))

    flagged = await pipeline.prepare(store, body(request), recent, PlanPolicy(context_threshold=1e-9))
    applied = await pipeline.prepare(
        store, body(request), recent, PlanPolicy(context_threshold=1e-9, threshold_auto_apply=True)
    )

    assert flagged.context_share is not None and flagged.context_share > 0 and flagged.plan_version is None
    assert applied.plan_version == 1
    assert context_share("claude-opus-5-5", 700_000) == 0.7 and context_share("unknown", 10) is None


# --- Major vs minor, and cost ------------------------------------------------------------------------


def test_versions_that_only_add_operations_are_minor() -> None:
    items = parse(conversation(4)).items
    first = summarize_operation(items, 0, 1, 1)
    second = summarize_operation(items, 2, 3, 2)

    assert is_minor(Plan(operations=(first,)), Plan(operations=(first, second)))
    assert not is_minor(Plan(operations=(first, second)), Plan(operations=(first,)))  # an undo
    assert is_minor(Plan(), Plan(operations=(first,)))


def test_cost_estimate_for_a_minor_compaction() -> None:
    items = parse(conversation(4)).items
    first = summarize_operation(items, 0, 1, 1)
    second = summarize_operation(items, 2, 3, 2)
    applied, pending = Plan(operations=(first,)), Plan(operations=(first, second))

    estimate = cost.estimate(items, applied, pending, DEFAULT_PRICING["claude-opus-5-5"])

    assert estimate.minor
    assert estimate.reprocessed_tokens == estimate.tokens_after - cost.frozen_tokens(applied)
    thinking_left = [i for i in items if i.kind is Kind.THINKING and i.msg_index > 3]
    assert estimate.dropped_thinking_blocks == len(thinking_left) == 2
    assert estimate.one_time_cost is not None and estimate.one_time_cost > 0
    assert cost.price_for("claude-opus-5-5", DEFAULT_PRICING) == DEFAULT_PRICING["claude-opus-5-5"]
    assert cost.price_for("claude-opus-5", DEFAULT_PRICING) == DEFAULT_PRICING["claude-opus-5"]  # longest prefix
    assert cost.price_for("unknown", DEFAULT_PRICING) is None


async def test_show_explains_what_applying_the_pending_version_costs(store: Store) -> None:
    request = conversation(4)
    prepared = await pipeline.prepare(store, body(request))
    assert prepared.session_id is not None
    offload = Operation(kind=OperationKind.OFFLOAD, item_ids=(parse(request).items[1].id,))
    await versions.create(store, prepared.session_id, (offload,))

    shown = await commands.show(store, prepared.session_id)

    assert "applying the pending version: minor" in shown
    assert "reasoning dropped: 4 thinking blocks" in shown


# --- Review findings ----------------------------------------------------------------------------


async def test_a_plan_applied_before_activation_points_existed_strips_all_and_records_one(store: Store) -> None:
    # Session 4a applied versions without recording where; their history's thinking all predates them.
    recent = RecentRequests()
    session = await with_summary_requested(store, recent, conversation(2))
    await pipeline.prepare(store, body(conversation(2)), recent)
    await store.run(
        lambda db: db.connection().exec_driver_sql(
            "UPDATE plans SET activated_at = NULL, activated_hash = NULL WHERE session_id = ?", (session,)
        )
    )

    upgraded = await pipeline.prepare(store, body(conversation(3)), recent)
    state = await versions.load_state(store, session)
    later = await pipeline.prepare(store, body(conversation(4)), recent)

    assert upgraded.outcome == "rendered" and thinking_texts(json.loads(upgraded.body)) == []
    assert state.activated_at == 7  # recorded on that clean turn
    assert thinking_texts(json.loads(later.body)) == ["reasoning 3"]  # produced after it


async def test_another_branch_leaves_out_all_its_completed_thinking(store: Store) -> None:
    # The version took effect on the main branch; this fork's thinking may predate it.
    recent = RecentRequests()
    await with_summary_requested(store, recent, conversation(2))
    await pipeline.prepare(store, body(conversation(2)), recent)  # takes effect at 5 messages
    fork = conversation(3)
    fork["messages"][3] = {"role": "assistant", "content": [thinking(9), {"type": "text", "text": "other answer"}]}

    prepared = await pipeline.prepare(store, body(fork), recent)

    assert prepared.outcome == "rendered" and prepared.violations == []
    assert thinking_texts(json.loads(prepared.body)) == []


async def test_an_older_request_resent_mid_tool_loop_keeps_its_loop_and_is_not_a_fallback(store: Store) -> None:
    recent = RecentRequests()
    await with_summary_requested(store, recent, conversation(3))
    await pipeline.prepare(store, body(conversation(3)), recent)  # takes effect at 7 messages
    older = conversation(1)
    older["messages"] += [
        {"role": "assistant", "content": [thinking(5), {"type": "tool_use", "id": "t1", "name": "ls", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ]

    prepared = await pipeline.prepare(store, body(older), recent)

    assert prepared.outcome == "rendered" and prepared.violations == []
    assert thinking_texts(json.loads(prepared.body)) == ["reasoning 5"]  # the loop in flight is untouched


async def test_redacted_thinking_is_left_out_like_thinking(store: Store) -> None:
    recent = RecentRequests()
    request = conversation(2)
    request["messages"][3]["content"].insert(0, {"type": "redacted_thinking", "data": "opaque"})
    await with_summary_requested(store, recent, request)

    sent = json.loads((await pipeline.prepare(store, body(request), recent)).body)

    assert all(block["type"] == "text" for block in sent["messages"][1]["content"])


@pytest.mark.parametrize(
    ("client_thinking", "expected"),
    [
        (
            {"type": "adaptive", "block_binding": {"mismatch_behavior": "drop_block"}},
            {"type": "adaptive", "block_binding": {"mismatch_behavior": "drop_block"}},  # one spelling only
        ),
        (
            {"type": "enabled", "budget_tokens": 2000},
            {"type": "enabled", "budget_tokens": 2000, "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
        ),
    ],
)
async def test_other_thinking_settings(store: Store, client_thinking: Any, expected: Any) -> None:
    recent = RecentRequests()
    request = conversation(2, model="claude-opus-4-6", thinking=client_thinking)
    await with_summary_requested(store, recent, request)

    prepared = await pipeline.prepare(store, body(request), recent)

    assert prepared.outcome == "rendered" and json.loads(prepared.body)["thinking"] == expected


def test_a_message_with_only_stale_thinking_makes_the_plan_not_fit() -> None:
    request = conversation(2)
    request["messages"][1]["content"] = [thinking(0)]  # e.g. cut off by max_tokens while thinking
    parsed = parse(request)
    offload = Operation(kind=OperationKind.OFFLOAD, item_ids=(parsed.items[0].id,))

    with pytest.raises(PlanNotApplicable, match="empty"):
        render(parsed, Plan(operations=(offload,)), stale_thinking_before=5)


async def test_promoting_a_version_that_is_no_longer_pending_does_nothing(store: Store) -> None:
    recent = RecentRequests()
    session = await with_summary_requested(store, recent, conversation(2))
    await versions.create(store, session, (summarize_operation(parse(conversation(4)).items, 2, 3, 2),))

    await versions.promote_pending(store, session, 1, (5, "a" * 64))  # version 2 replaced it meanwhile
    state = await versions.load_state(store, session)

    assert state.applied.number == 0 and state.pending is not None and state.pending.number == 2


def test_idle_time_reads_stored_times_as_utc() -> None:
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

    assert seconds_between(datetime(2026, 11, 4, 11, 55), now) == 300.0  # how SQLite gives it back
    assert seconds_between(datetime(2026, 11, 4, 11, 59, tzinfo=UTC), now) == 60.0


def test_beta_merging_with_several_empty_and_differently_cased_headers() -> None:
    headers = [(b"anthropic-beta", b"one"), (b"anthropic-beta", b"Thinking-Binding-Controls-2026-08-01")]
    assert with_betas(list(headers), [BINDING_BETA]) == headers  # already there, any case
    assert with_betas([(b"anthropic-beta", b"")], [BINDING_BETA]) == [(b"anthropic-beta", BINDING_BETA.encode())]
    assert with_betas([(b"anthropic-beta", b"a"), (b"anthropic-beta", b"b")], [BINDING_BETA])[-1] == (
        b"anthropic-beta",
        b"b," + BINDING_BETA.encode(),
    )


# --- Fallbacks in an edited session (hard rule 1 amendment) -------------------------------------


async def edited_then_failing(store: Store, monkeypatch: pytest.MonkeyPatch, **request: Any) -> pipeline.Prepared:
    """A session where version 1 took effect, then a request on which rendering fails."""
    recent = RecentRequests()
    await with_summary_requested(store, recent, conversation(2, **request))
    await pipeline.prepare(store, body(conversation(2, **request)), recent)

    def broken(*args: Any) -> Any:
        raise RuntimeError("a bug in rendering")

    monkeypatch.setattr(pipeline, "render", broken)
    return await pipeline.prepare(store, body(conversation(3, **request)), recent)


async def test_a_fallback_in_an_edited_session_adds_the_opt_in_to_the_clients_own_request(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = await edited_then_failing(store, monkeypatch)
    sent = json.loads(prepared.body)

    assert prepared.outcome == "fallback"
    assert sent["messages"] == conversation(3)["messages"]  # the client's messages, thinking included
    assert sent["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert prepared.betas == (BINDING_BETA,)
    assert prepared.error is not None and "RuntimeError" in prepared.error and "opt-in" in prepared.error


@pytest.mark.parametrize(
    "request_settings",
    [
        {"thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}}},  # client's choice
        {"model": "some-future-model"},  # not confirmed
        {"model": "claude-sonnet-5-5", "thinking": {"type": "between_tools"}},  # takes no other field
    ],
)
async def test_a_fallback_without_the_conditions_sends_the_bytes_as_they_came(
    store: Store, monkeypatch: pytest.MonkeyPatch, request_settings: dict[str, Any]
) -> None:
    prepared = await edited_then_failing(store, monkeypatch, **request_settings)

    assert prepared.outcome == "fallback" and prepared.betas == ()
    assert prepared.body == body(conversation(3, **request_settings))


async def test_a_fallback_in_a_session_never_edited_sends_the_bytes_as_they_came(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any) -> Any:
        raise RuntimeError("a bug in rendering")

    monkeypatch.setattr(pipeline, "render", broken)
    raw = body(conversation(3))

    prepared = await pipeline.prepare(store, raw, RecentRequests())

    assert prepared.outcome == "fallback" and prepared.body is raw and prepared.betas == ()
