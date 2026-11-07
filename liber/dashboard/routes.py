"""The dashboard's pages and actions, served by the proxy's own app under /liber/ (ARCHITECTURE.md §10, D11).

    GET  /liber/                              sessions, with their branches
    GET  /liber/sessions/{id}                 one session: plan, topic map, items, usage chart
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
from sqlalchemy import select

from liber.config import Settings
from liber.dashboard import views
from liber.plan import commands, versions
from liber.plan.versions import PlanError
from liber.session.store import BranchRecord, RequestRecord, Store

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
    by_session: dict[str, list[BranchRecord]] = {}
    for branch in branches:
        by_session.setdefault(branch.session_id, []).append(branch)
    states = {session.id: await versions.load_state(store, session.id) for session in sessions}
    return page(
        request,
        "index.html",
        {
            "sessions": sessions,
            "branches": by_session,
            "states": states,
            "worker_on": request.app.state.worker is not None,
        },
    )


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
        "message": message,
        "error": error,
        "worker_on": request.app.state.worker is not None,
    }
    return page(request, template, context)


def page(request: Request, template: str, context: dict[str, Any]) -> Response:
    response = templates.TemplateResponse(request, template, context)
    response.headers.update(SECURITY_HEADERS)
    return response
