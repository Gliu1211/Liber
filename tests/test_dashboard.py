"""The dashboard (liber/dashboard/, ARCHITECTURE.md §10, D11; Session 6 acceptance criteria).

Route tests for every action, through the same app the proxy runs, with a fake upstream and a
fake worker model (no network, hard rule 7).
"""

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from liber.model.anthropic import parse
from liber.plan import commands, versions
from liber.session.store import Store
from tests.helpers import FakeModel, FakeUpstream, recorded, summarize_now

LONG = " The details: the build cache keeps every artifact keyed by its inputs, so a rebuild reuses them." * 8
TURNS = ["Set up the build" + LONG, "Done." + LONG, "Now the tests" + LONG, "They pass." + LONG, "Ship it"]
HX = {"HX-Request": "true"}


def conversation(turns: list[str] = TURNS) -> dict[str, Any]:
    roles = ["user", "assistant"]
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 100,
        "messages": [{"role": roles[i % 2], "content": text} for i, text in enumerate(turns)],
    }


def reply_with_usage(input_tokens: int, cache_read: int, cache_write: int) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "output_tokens": 2,
        },
    }


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # The dashboard only answers on a loopback host name (DNS rebinding guard), as a browser on
    # this machine would send it.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8484") as client:
        yield client


async def a_session(app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream, summarized: bool = True) -> str:
    """A recorded session (two requests, with usage), its exchanges summarized by the worker."""
    usages = iter([reply_with_usage(1200, 0, 1100), reply_with_usage(40, 1100, 300)])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(usages))

    upstream.handler = handler
    for turns in (TURNS[:3], TURNS):
        await client.post("/v1/messages", content=json.dumps(conversation(turns)).encode())
    [*_, last] = await recorded(app)
    assert last.session_id is not None
    if summarized:
        await summarize_now(app.state.store, last.session_id, parse(conversation()))
    return last.session_id


async def test_the_session_list_shows_sessions_with_their_branches(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    session = await a_session(app, client, upstream)
    fork = conversation([*TURNS[:3], "A different answer", "And on"])
    await client.post("/v1/messages", content=json.dumps(fork).encode())
    await recorded(app)

    page = await client.get("/liber/")

    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert session in page.text
    assert page.text.count('class="branch"') == 2  # the original and the fork
    assert f'href="/liber/sessions/{session}"' in page.text


async def test_the_session_page_shows_items_topics_and_the_usage_chart(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    session = await a_session(app, client, upstream)
    view = await commands.session_view(app.state.store, session)

    page = await client.get(f"/liber/sessions/{session}")

    assert page.status_code == 200
    # Item table: one row per item, with role, kind, tokens, topic and status.
    rows = re.findall(r'<tr class="item"', page.text)
    assert len(rows) == len(view.items)
    first_topic = view.summaries[view.exchanges[0].key].topic
    assert first_topic in page.text and "kept" in page.text
    # Topic map: each summarized exchange's topic with its token total; the turn in progress too.
    first = view.exchanges[0]
    total = view.exact[first.key]  # the worker's exact count (no thinking in it), not the estimate
    assert total != sum(item.tokens for item in first.items)
    assert re.search(rf"{re.escape(first_topic)}</td>\s*<td[^>]*>{total:,}<", page.text)
    assert "current turn" in page.text
    # Chart: input tokens, cache reads and cache writes per request, drawn on the server.
    assert page.text.count("<svg") == 1
    assert {"uncached input", "cache reads", "cache writes"} <= set(re.findall(r'data-series="([^"]+)"', page.text))


async def test_summarize_creates_a_pending_version_with_a_cost_preview(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    session = await a_session(app, client, upstream)

    response = await client.post(
        f"/liber/sessions/{session}/plan", data={"action": "summarize", "through": "1"}, headers=HX
    )

    state = await versions.load_state(app.state.store, session)
    assert response.status_code == 200
    assert state.pending is not None and state.pending.number == 1
    assert "Created plan version 1" in response.text
    assert "pays back" in response.text or "never" in response.text  # the §6 preview
    assert "tokens per request" in response.text


async def test_a_summary_the_worker_has_not_written_is_refused_in_the_page(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    session = await a_session(app, client, upstream, summarized=False)

    response = await client.post(
        f"/liber/sessions/{session}/plan", data={"action": "summarize", "through": "1"}, headers=HX
    )

    assert response.status_code == 200
    assert "not ready" in response.text and 'class="error"' in response.text
    assert (await versions.load_state(app.state.store, session)).pending is None


async def test_offload_and_delete_selected_items(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    session = await a_session(app, client, upstream)
    view = await commands.session_view(app.state.store, session)
    long_reply = view.items[1].id  # message 1: the assistant's long reply
    long_question = view.items[2].id

    offloaded = await client.post(
        f"/liber/sessions/{session}/plan",
        data={"action": "offload", "items": [long_reply], "note": "old <answer>"},
        headers=HX,
    )
    deleted = await client.post(
        f"/liber/sessions/{session}/plan", data={"action": "delete", "items": [long_question]}, headers=HX
    )

    state = await versions.load_state(app.state.store, session)
    assert offloaded.status_code == deleted.status_code == 200
    assert state.pending is not None and state.pending.number == 2
    kinds = [(op.kind, op.item_ids) for op in state.pending.plan.operations]
    assert kinds == [("offload", (long_reply,)), ("delete", (long_question,))]
    assert state.pending.plan.operations[0].note == "old <answer>"
    assert "old &lt;answer&gt;" in deleted.text and "old <answer>" not in deleted.text  # shown escaped


async def test_apply_and_undo(app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    session = await a_session(app, client, upstream)
    await client.post(f"/liber/sessions/{session}/plan", data={"action": "summarize", "through": "1"}, headers=HX)

    nothing_to_undo = await client.post(f"/liber/sessions/{session}/undo", headers=HX)
    applied = await client.post(f"/liber/sessions/{session}/apply", headers=HX)
    assert (await versions.load_state(app.state.store, session)).apply_requested
    await client.post("/v1/messages", content=json.dumps(conversation()).encode())  # the next clean turn
    await recorded(app)
    in_use = await client.get(f"/liber/sessions/{session}")
    undone = await client.post(f"/liber/sessions/{session}/undo", headers=HX)
    after_undo = await versions.load_state(app.state.store, session)

    assert 'class="error"' in nothing_to_undo.text and "nothing to undo" in nothing_to_undo.text
    assert applied.status_code == undone.status_code == 200
    assert "will be applied" in applied.text
    assert "applied plan version: 1" in in_use.text and "summarized (v1)" in in_use.text
    assert "Going back to plan version 0" in undone.text
    assert after_undo.pending is not None and after_undo.pending.number == 0 and after_undo.apply_requested


async def test_every_action_goes_through_the_cli_plan_code(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await a_session(app, client, upstream)
    calls: list[tuple[str, Any]] = []

    async def fake_create(store: Store, session_id: str, *args: Any, **kwargs: Any) -> str:
        calls.append(("create", (session_id, args, kwargs)))
        return "created"

    async def fake_apply(store: Store, session_id: str) -> str:
        calls.append(("apply", session_id))
        return "applied"

    async def fake_undo(store: Store, session_id: str) -> str:
        calls.append(("undo", session_id))
        return "undone"

    monkeypatch.setattr(commands, "create", fake_create)
    monkeypatch.setattr(commands, "apply", fake_apply)
    monkeypatch.setattr(commands, "undo", fake_undo)
    await client.post(f"/liber/sessions/{session}/plan", data={"action": "summarize", "through": "1"}, headers=HX)
    await client.post(f"/liber/sessions/{session}/apply", headers=HX)
    await client.post(f"/liber/sessions/{session}/undo", headers=HX)

    assert [name for name, _ in calls] == ["create", "apply", "undo"]
    _, (_, args, kwargs) = calls[0]
    assert args[0] == 1 and kwargs["summary_model"] == app.state.settings.summary_model


async def test_bad_form_input_is_a_message_not_an_error(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    session = await a_session(app, client, upstream)

    no_through = await client.post(f"/liber/sessions/{session}/plan", data={"action": "summarize"}, headers=HX)
    not_a_number = await client.post(
        f"/liber/sessions/{session}/plan", data={"action": "summarize", "through": "x"}, headers=HX
    )
    nothing_selected = await client.post(f"/liber/sessions/{session}/plan", data={"action": "offload"}, headers=HX)
    unknown = await client.post(f"/liber/sessions/{session}/plan", data={"action": "launch"}, headers=HX)
    nothing_pending = await client.post(f"/liber/sessions/{session}/apply", headers=HX)

    for response in (no_through, not_a_number, nothing_selected, unknown, nothing_pending):
        assert response.status_code == 200 and 'class="error"' in response.text


async def test_an_unknown_session_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/liber/sessions/nope")).status_code == 404


# --- safety: only this machine's browser, only this page --------------------------------------------


async def test_other_host_names_are_refused(app: FastAPI, upstream: FakeUpstream) -> None:
    # DNS rebinding: a site whose name resolves to 127.0.0.1 sends its own name as Host.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://evil.example:8484") as client:
        page = await client.get("/liber/")
    assert page.status_code == 403


@pytest.mark.parametrize("action", ["plan", "apply", "undo"])
async def test_actions_need_htmx_and_the_same_origin(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream, action: str
) -> None:
    session = await a_session(app, client, upstream)
    url = f"/liber/sessions/{session}/{action}"
    form = {"action": "summarize", "through": "1"}

    # A form on another site can post here but can't add headers without CORS, which Liber never grants.
    plain_form = await client.post(url, data=form)
    other_origin = await client.post(url, data=form, headers={**HX, "Origin": "https://evil.example"})
    sandboxed = await client.post(url, data=form, headers={**HX, "Origin": "null"})
    same_origin = await client.post(url, data=form, headers={**HX, "Origin": "http://127.0.0.1:8484"})

    assert plain_form.status_code == other_origin.status_code == sandboxed.status_code == 403
    assert same_origin.status_code == 200


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("127.0.0.1:8484", True),
        ("localhost:8484", True),
        ("[::1]:8484", True),
        ("127.0.0.1", True),
        ("127.0.0.1.evil.example:8484", False),
        ("localhost.evil.example", False),
        ("evil.example", False),
    ],
)
async def test_only_loopback_host_names_are_answered(client: httpx.AsyncClient, host: str, allowed: bool) -> None:
    page = await client.get("/liber/", headers={"host": host})

    assert (page.status_code == 200) is allowed


async def test_no_other_site_can_frame_the_pages(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    session = await a_session(app, client, upstream)

    for response in (
        await client.get("/liber/"),
        await client.get(f"/liber/sessions/{session}"),
        await client.post(f"/liber/sessions/{session}/apply", headers=HX),
    ):
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "default-src 'self'" in response.headers["content-security-policy"]


async def test_the_page_loads_nothing_from_the_internet(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    session = await a_session(app, client, upstream)
    pages = [await client.get("/liber/"), await client.get(f"/liber/sessions/{session}")]
    script = await client.get("/liber/static/htmx.min.js")

    assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
    assert script.text.startswith("var htmx=")
    for page in pages:
        assert not re.search(r'(src|href)="(https?:)?//', page.text)  # local URLs only
        assert '<script src="/liber/static/htmx.min.js"></script>' in page.text


async def test_text_from_the_worker_is_escaped(app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    session = await a_session(app, client, upstream, summarized=False)

    model = FakeModel()
    model.reply = lambda call: {"topic": "<script>alert(1)</script>", "summary": "s"}
    await summarize_now(app.state.store, session, parse(conversation()), model=model)

    page = await client.get(f"/liber/sessions/{session}")

    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


async def test_dashboard_paths_are_never_forwarded_upstream(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    session = await a_session(app, client, upstream)
    sent_before = len(upstream.requests)

    answers = [
        await client.get("/liber/"),
        await client.get("/liber/sessions/nope"),
        await client.get("/liber/static/htmx.min.js"),
        await client.get("/liber/no-such-page"),
        await client.get(f"/liber/sessions/{session}/apply"),  # a POST route, asked with GET
        await client.options(f"/liber/sessions/{session}/apply"),  # a CORS preflight
        await client.delete(f"/liber/sessions/{session}"),
    ]

    assert [r.status_code for r in answers] == [200, 404, 200, 404, 404, 404, 404]
    assert len(upstream.requests) == sent_before  # nothing under /liber/ went upstream


async def test_an_action_on_an_unknown_session_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/liber/sessions/nope/plan", data={"action": "summarize", "through": "1"}, headers=HX)

    assert response.status_code == 404


@pytest.mark.parametrize("through", ["²", "①", "1" * 5000, "-1", " "])
async def test_a_message_number_that_isnt_one_is_a_message(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream, through: str
) -> None:
    session = await a_session(app, client, upstream)

    response = await client.post(
        f"/liber/sessions/{session}/plan", data={"action": "summarize", "through": through}, headers=HX
    )

    assert response.status_code == 200 and 'class="error"' in response.text


async def test_thinking_items_are_refused_even_when_posted_directly(
    app: FastAPI, client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    turns: list[Any] = list(TURNS)
    turns[1] = [{"type": "thinking", "thinking": "hmm", "signature": "sig"}, {"type": "text", "text": TURNS[1]}]
    await client.post("/v1/messages", content=json.dumps(conversation(turns)).encode())
    [*_, last] = await recorded(app)
    assert last.session_id is not None
    view = await commands.session_view(app.state.store, last.session_id)
    thinking = next(item.id for item in view.items if item.kind == "thinking")

    page = await client.get(f"/liber/sessions/{last.session_id}")
    response = await client.post(
        f"/liber/sessions/{last.session_id}/plan", data={"action": "delete", "items": [thinking]}, headers=HX
    )

    assert re.search(rf'value="{thinking}"[^>]*disabled', page.text)  # can't be ticked in the page
    assert "thinking blocks are never offloaded or deleted" in response.text
    assert (await versions.load_state(app.state.store, last.session_id)).pending is None
