"""The dashboard's pages and actions, served by the proxy's own app under /liber/ (ARCHITECTURE.md §10, D11).

    GET  /liber/                              sessions, with their branches and context used
    GET  /liber/sessions/{id}                 one session: context meter, plan, topic map, items, usage chart
    POST /liber/sessions/{id}/plan            offload / summarize / delete -> a pending version
    POST /liber/sessions/{id}/apply           apply the pending version at the next clean turn
    POST /liber/sessions/{id}/undo            go back one version, at the next clean turn
    GET  /liber/static/htmx.min.js            HTMX, shipped with Liber (nothing loads from the internet)

Every action calls plan/commands.py, the code behind `liber plan`, and answers with the session's
panel re-rendered (HTMX swaps it in place), with the command's message or its refusal.

Who may use it (docs/decisions/0014). The proxy listens on 127.0.0.1, but any web page open in the
user's browser can still send requests there:
- **Host must be a loopback name** (127.0.0.1, localhost, [::1]). A site that points its own name
  at 127.0.0.1 (DNS rebinding) sends that name, and is refused, so it can't read the pages.
- **Actions need the `HX-Request` header**, which HTMX adds. A form on another site can post
  here but can't add a header; a script can only add one after a CORS check, which Liber never
  grants. And if the browser says where the request came from (`Origin`), it must be this page.
- **No other site may frame the pages** (`X-Frame-Options`, CSP `frame-ancestors`), so it can't
  trick a click on Apply or Undo through an invisible frame. The CSP also lets the page load
  scripts only from Liber itself.
- **Every /liber/ path is answered here** (`not_found` catches the rest), so none of them is ever
  forwarded upstream.
"""

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from liber.config import Settings
from liber.dashboard import views
from liber.plan import commands, versions
from liber.plan.versions import PlanError
from liber.session.store import BranchRecord, ChainNode, RequestRecord, Store

HERE = Path(__file__).parent
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "[::1]"}
HTMX_PATH = HERE / "static" / "htmx.min.js"
SECURITY_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
}

templates = Jinja2Templates(directory=HERE / "templates")  # autoescapes .html: worker text is data


def loopback_host(request: Request) -> None:
    """Refuse any Host but this machine's own names (DNS rebinding)."""
    host = request.headers.get("host", "")
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    if name.lower() not in LOOPBACK_NAMES:
        raise HTTPException(403, "the Liber dashboard only answers on 127.0.0.1 or localhost")


def from_this_page(request: Request) -> None:
    """Refuse actions that didn't come from the dashboard itself (another site's form or script)."""
    if request.headers.get("hx-request") != "true":
        raise HTTPException(403, "dashboard actions are sent by the dashboard page")
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "dashboard actions are sent by the dashboard page")


router = APIRouter(prefix="/liber", dependencies=[Depends(loopback_host)])


def store_of(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


def settings_of(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    store = store_of(request)
    sessions = list(reversed(await store.list_sessions()))  # newest first
    branches = await store.run(lambda db: list(db.scalars(select(BranchRecord).order_by(BranchRecord.created_at))))
    nodes = await store.run(lambda db: branch_head_nodes(db, [branch.head_hash for branch in branches]))
    side = views.side_branches(branches, nodes)
    by_session: dict[str, list[BranchRecord]] = {}
    side_by_session: dict[str, list[BranchRecord]] = {}
    for branch in branches:
        listing = side_by_session if branch.id in side else by_session
        listing.setdefault(branch.session_id, []).append(branch)
    states = {session.id: await versions.load_state(store, session.id) for session in sessions}
    latest = await store.run(latest_requests)
    meters = {}
    for session in sessions:
        record = latest.get(session.id)
        applied = states[session.id].applied
        items = None
        # Only a request Liber edited with the applied version needs the items, for its saving.
        if record is not None and record.liber_outcome == "rendered" and record.plan_version == applied.number:
            try:
                view = await commands.session_view(store, session.id, summary_model=settings_of(request).summary_model)
                items = views.counted_items(view)
            except PlanError:  # no items stored for it: the line shows the totals only
                items = None
        meters[session.id] = views.context_meter(record, applied, items)
    return page(
        request,
        "index.html",
        {
            "sessions": sessions,
            "branches": by_session,
            "side_branches": side_by_session,
            "states": states,
            "meters": meters,
            "compact": views.compact,
            "percent": views.percent,
            "worker_on": request.app.state.worker is not None,
        },
    )


def latest_requests(db: Session) -> dict[str, RequestRecord]:
    """Each session's newest request with usage, in one query."""
    newest = (
        select(func.max(RequestRecord.id))
        .where(RequestRecord.session_id.is_not(None), RequestRecord.input_tokens.is_not(None))
        .group_by(RequestRecord.session_id)
    )
    records = db.scalars(select(RequestRecord).where(RequestRecord.id.in_(newest)))
    return {record.session_id: record for record in records if record.session_id is not None}


def branch_head_nodes(db: Session, heads: list[str]) -> list[views.NodeFacts]:
    """What views.side_branches needs: the branch heads, and the children of heads and of their
    parents. Two small queries rather than every stored message."""
    columns = (ChainNode.hash, ChainNode.parent_hash, ChainNode.session_id, ChainNode.branch_id, ChainNode.created_at)
    head_rows = list(db.execute(select(*columns).where(ChainNode.hash.in_(heads)))) if heads else []
    near = {row.hash for row in head_rows} | {row.parent_hash for row in head_rows}
    child_rows = list(db.execute(select(*columns).where(ChainNode.parent_hash.in_(near)))) if near else []
    rows = {row.hash: row for row in [*head_rows, *child_rows]}
    return [
        views.NodeFacts(row.hash, row.parent_hash, row.session_id, row.branch_id, row.created_at)
        for row in rows.values()
    ]


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_page(request: Request, session_id: str) -> Response:
    return await render_session(request, session_id, "session.html")


@router.post("/sessions/{session_id}/plan", response_class=HTMLResponse, dependencies=[Depends(from_this_page)])
async def change_plan(request: Request, session_id: str) -> Response:
    """Offload or delete the selected items, or summarize through a message: a new pending version."""
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    action = form.get("action", [""])[0]
    selected = tuple(form.get("items", []))
    note = form.get("note", [""])[0].strip() or None
    settings = settings_of(request)
    store = store_of(request)
    message = error = None
    try:
        if action == "summarize":
            message = await commands.create(
                store,
                session_id,
                message_number(form.get("through", [""])[0]),
                summary_model=settings.summary_model,
                carry_forward_reasoning=settings.carry_forward_reasoning,
            )
        elif action in ("offload", "delete"):
            if not selected:
                raise PlanError(f"select the items to {action}")
            offload, delete = (selected, ()) if action == "offload" else ((), selected)
            message = await commands.create(
                store,
                session_id,
                None,
                offload,
                delete,
                note,
                summary_model=settings.summary_model,
                carry_forward_reasoning=settings.carry_forward_reasoning,
            )
        else:
            raise PlanError("choose offload, summarize or delete")
    except PlanError as exc:
        error = str(exc)
    return await render_session(request, session_id, "_session.html", message, error)


def message_number(text: str) -> int:
    """A message index from the form; PlanError (shown in the page) if it isn't one."""
    text = text.strip()
    if not (text.isascii() and text.isdecimal() and len(text) <= 9):
        raise PlanError("choose the message to summarize through")
    return int(text)


@router.post("/sessions/{session_id}/apply", response_class=HTMLResponse, dependencies=[Depends(from_this_page)])
async def apply(request: Request, session_id: str) -> Response:
    message = error = None
    try:
        message = await commands.apply(store_of(request), session_id)
    except PlanError as exc:
        error = str(exc)
    return await render_session(request, session_id, "_session.html", message, error)


@router.post("/sessions/{session_id}/undo", response_class=HTMLResponse, dependencies=[Depends(from_this_page)])
async def undo(request: Request, session_id: str) -> Response:
    message = error = None
    try:
        message = await commands.undo(store_of(request), session_id)
    except PlanError as exc:
        error = str(exc)
    return await render_session(request, session_id, "_session.html", message, error)


@router.get("/static/htmx.min.js")
async def htmx() -> Response:
    # Read when asked, not at import: a missing file breaks this page, never the proxy (hard rule 1).
    try:
        script = HTMX_PATH.read_bytes()
    except OSError as exc:
        raise HTTPException(404, "htmx.min.js is missing from this install") from exc
    return Response(
        script, media_type="text/javascript", headers={"cache-control": "max-age=86400", **SECURITY_HEADERS}
    )


@router.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def not_found(rest: str) -> Response:
    """Any other /liber/ path or method: answered here, never forwarded upstream. Registered last."""
    raise HTTPException(404, "no such dashboard page")


async def render_session(
    request: Request, session_id: str, template: str, message: str | None = None, error: str | None = None
) -> Response:
    store, settings = store_of(request), settings_of(request)
    try:
        view = await commands.session_view(store, session_id, settings.pricing, settings.summary_model)
    except PlanError as exc:
        raise HTTPException(404, str(exc)) from exc
    requests = await store.run(
        lambda db: list(
            db.scalars(select(RequestRecord).where(RequestRecord.session_id == session_id).order_by(RequestRecord.id))
        )
    )
    context = {
        "view": view,
        "rows": views.item_rows(view),
        "topics": views.topic_map(view),
        "ends": views.summary_ends(view),
        "estimate": views.estimate_lines(view),
        "versions": views.plan_lines(view),
        "chart": views.usage_chart(requests),
        "meter": views.context_meter(views.latest_with_usage(requests), view.state.applied, views.counted_items(view)),
        "compact": views.compact,
        "percent": views.percent,
        "message": message,
        "error": error,
        "worker_on": request.app.state.worker is not None,
    }
    return page(request, template, context)


def page(request: Request, template: str, context: dict[str, Any]) -> Response:
    response = templates.TemplateResponse(request, template, context)
    response.headers.update(SECURITY_HEADERS)
    return response
