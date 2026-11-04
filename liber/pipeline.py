"""The request path between receiving and forwarding: ARCHITECTURE.md §3 steps 2-5.

For every POST /v1/messages, `prepare()` decides which bytes go upstream:

    1. parse the JSON into items, reusing the    (model/anthropic.py)
       work done for this conversation's previous
       request (RecentRequests, ADR 0007)
    2. identify the conversation and register
       the new messages and items                (session/chain.py)
    3. render with the session's plan: a pending  (plan/policy.py, plan/versions.py,
       version requested by the user takes        plan/apply.py)
       effect at a clean turn boundary, else the
       applied one; a plan that doesn't fit this
       request is skipped (edit_skipped says why)
    4. validate the rendered request             (model/validate.py)
    5. choose what to send:
         "passthrough"  nothing changed, or an   -> the client's exact bytes
                        edit was held back
         "rendered"     a plan changed it        -> the rendered request
         "fallback"     anything went wrong      -> the client's exact bytes, and the reason

(§3 lists identification before parsing; here parsing comes first because the chain hashes that
identify the conversation are computed from the parsed messages.)

Held back until Session 4b (docs/decisions/0009): an edited request that still contains thinking
blocks. On current models editing the history before a thinking block invalidates it (ADR 0004),
and the opt-in that makes the API drop such blocks instead of rejecting the request arrives in
4b. Until then those requests go through unedited, with the reason recorded.

Hard rule 1: `prepare()` never raises and never returns anything but the client's bytes unless
a non-empty plan produced a request that passed validation.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from liber.model.anthropic import ParsedRequest, RecentRequests, RequestShapeError, parse, render
from liber.model.items import Item, OperationKind
from liber.model.validate import THINKING_TYPES, Violation, validate
from liber.plan import policy, versions
from liber.plan.apply import PlanNotApplicable
from liber.session.chain import StoreBusy, identify
from liber.session.store import Store

log = structlog.get_logger(__name__)

HELD_FOR_THINKING = (
    "edit held back: the request has thinking blocks, which need preserved-thinking support (Session 4b)"
)


@dataclass
class Prepared:
    """What to send upstream, and what Liber learned and did on the way (recorded per request)."""

    body: bytes
    outcome: str = "fallback"  # "passthrough", "rendered" or "fallback"
    request: Any = None  # the decoded client request (None if it wasn't JSON), for recording
    rendered: dict[str, Any] | None = None  # what render() produced, whether or not it was sent
    session_id: str | None = None
    branch_id: str | None = None
    known_messages: int | None = None
    new_messages: int | None = None
    error: str | None = None  # why it fell back (an exception type, or a round-trip mismatch)
    violations: list[Violation] = field(default_factory=list)
    overhead_ms: float | None = None
    plan_version: int | None = None  # the plan version rendered (None: no plan)
    edit_skipped: str | None = None  # why a plan wasn't used, or its edit wasn't sent
    offloaded: list[Item] = field(default_factory=list)  # items whose originals go to the blob store


async def prepare(store: Store, raw_body: bytes, recent: RecentRequests | None = None) -> Prepared:
    """Run §3 steps 2-5 on a request body. Never raises; falls back to `raw_body` on any problem.

    `recent`: the app's cache of recent conversations, so only new messages are hashed (ADR 0007).
    Without it every message is hashed, with the same result.
    """
    started = time.perf_counter()
    prepared = Prepared(body=raw_body)
    try:
        request = json.loads(raw_body)
        prepared.request = request
        # A second, independent copy for validation: render() passes the client's block objects
        # through, so comparing against those same objects could not catch a render bug that
        # edits one in place.
        reference = json.loads(raw_body)
        # find, parse and remember run with no await in between, so concurrent requests can't
        # interleave here.
        previous = recent.find(request) if recent is not None else None
        parsed = parse(request, previous)
        if recent is not None:
            recent.remember(parsed, replacing=previous)
        identity = await identify(store, parsed)
        prepared.session_id, prepared.branch_id = identity.session_id, identity.branch_id
        prepared.known_messages, prepared.new_messages = identity.known_messages, identity.new_messages

        state = identity.plan
        used, rendered = render_with_plan(parsed, state, prepared)
        prepared.rendered = rendered
        prepared.violations = validate(rendered, original=reference)
        if used.number:
            prepared.plan_version = used.number
            prepared.offloaded = offloaded_items(parsed, used)

        if prepared.violations:
            pass  # stay on "fallback": the violations say why
        elif used.plan.is_empty and rendered != reference:
            prepared.error = "round trip changed the request under an empty plan (a parse/render bug)"
        elif used.plan.is_empty or rendered == reference:
            prepared.outcome = "passthrough"  # nothing changed: send the client's exact bytes
        else:
            if state.pending is not None and used.number == state.pending.number and identity.session_id:
                # The requested version fits and is valid at a clean turn boundary: it is now applied.
                await versions.promote_pending(store, identity.session_id, used.number)
            if has_thinking(rendered):
                prepared.outcome = "passthrough"
                prepared.edit_skipped = HELD_FOR_THINKING
            else:
                prepared.outcome = "rendered"
                prepared.body = encode_request(rendered)
    except Exception as exc:
        prepared.error = describe_failure(exc)

    prepared.overhead_ms = round((time.perf_counter() - started) * 1000, 3)
    if prepared.outcome == "fallback":
        log.warning(
            "liber_fallback",
            error=prepared.error,
            violations=[v.rule for v in prepared.violations],
            session_id=prepared.session_id,
        )
    return prepared


def render_with_plan(
    parsed: ParsedRequest, state: versions.PlanState, prepared: Prepared
) -> tuple[versions.PlanVersion, dict[str, Any]]:
    """Render with the first plan version (policy.versions_to_try) that fits this request.

    The last candidate is always the empty plan, which fits every request.
    """
    for version in policy.versions_to_try(state, parsed.original["messages"]):
        try:
            return version, render(parsed, version.plan)
        except PlanNotApplicable as exc:
            prepared.edit_skipped = f"plan version {version.number} not used: {exc}"
    raise AssertionError("the empty plan always fits")  # unreachable: versions_to_try ends with it


def offloaded_items(parsed: ParsedRequest, version: versions.PlanVersion) -> list[Item]:
    """This request's items that the plan offloads, so their originals can be stored (§4)."""
    ids = {
        item_id
        for operation in version.plan.operations
        if operation.kind is OperationKind.OFFLOAD
        for item_id in operation.item_ids
    }
    return [item for item in parsed.items if item.id in ids]


def has_thinking(request: dict[str, Any]) -> bool:
    return any(
        isinstance(block, dict) and block.get("type") in THINKING_TYPES
        for message in request["messages"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
    )


def encode_request(request: dict[str, Any]) -> bytes:
    """The bytes Liber sends for a request it rendered.

    One fixed encoding (key order as built, no spaces, UTF-8 as-is) so the same history and plan
    always produce the same bytes: preserved thinking and the prompt cache both depend on it
    (ADR 0004 A2; measured by the Tier 1 metric render_determinism).
    """
    return json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def describe_failure(exc: Exception) -> str:
    """The exception type, plus its message only when Liber wrote it.

    Other exceptions' messages can quote pieces of the conversation (a KeyError names a key,
    a ValueError may repeat a value), and errors are stored and logged even when body
    recording is off.
    """
    if isinstance(exc, json.JSONDecodeError | RequestShapeError | StoreBusy | PlanNotApplicable):
        return f"{type(exc).__name__}: {exc}"  # positions and Liber's own wording, no content
    return type(exc).__name__
