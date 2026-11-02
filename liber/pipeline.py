"""The request path between receiving and forwarding: ARCHITECTURE.md §3 steps 2-5.

For every POST /v1/messages, `prepare()` decides which bytes go upstream:

    1. parse the JSON into items                 (model/anthropic.py)
    2. identify the conversation and register
       the new messages and items                (session/chain.py)
    3. render with the session's applied plan    (empty until Session 4 stores plans)
    4. validate the rendered request             (model/validate.py)
    5. choose what to send:
         "passthrough"  nothing changed          -> the client's exact bytes
         "rendered"     a plan changed it        -> the rendered request (Session 4 onwards)
         "fallback"     anything went wrong      -> the client's exact bytes, and the reason

(§3 lists identification before parsing; here parsing comes first because the chain hashes that
identify the conversation are computed from the parsed messages.)

Hard rule 1: `prepare()` never raises and never returns anything but the client's bytes unless
a non-empty plan produced a request that passed validation.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from liber.model.anthropic import RequestShapeError, parse, render
from liber.model.items import Plan
from liber.model.validate import Violation, validate
from liber.session.chain import StoreBusy, identify
from liber.session.store import Store

log = structlog.get_logger(__name__)


@dataclass
class Prepared:
    """What to send upstream, and what Liber learned and did on the way (recorded per request)."""

    body: bytes
    outcome: str = "fallback"  # "passthrough", "rendered" or "fallback"
    request: Any = None  # the decoded client request (None if it wasn't JSON), for recording
    session_id: str | None = None
    branch_id: str | None = None
    known_messages: int | None = None
    new_messages: int | None = None
    error: str | None = None  # why it fell back (an exception type, or a round-trip mismatch)
    violations: list[Violation] = field(default_factory=list)
    overhead_ms: float | None = None


async def prepare(store: Store, raw_body: bytes) -> Prepared:
    """Run §3 steps 2-5 on a request body. Never raises; falls back to `raw_body` on any problem."""
    started = time.perf_counter()
    prepared = Prepared(body=raw_body)
    try:
        request = json.loads(raw_body)
        prepared.request = request
        # A second, independent copy for validation: render() passes the client's block objects
        # through, so comparing against those same objects could not catch a render bug that
        # edits one in place.
        reference = json.loads(raw_body)
        parsed = parse(request)
        identity = await identify(store, parsed)
        prepared.session_id, prepared.branch_id = identity.session_id, identity.branch_id
        prepared.known_messages, prepared.new_messages = identity.known_messages, identity.new_messages

        plan = Plan()  # the session's applied plan; plans are stored from Session 4
        rendered = render(parsed, plan)
        prepared.violations = validate(rendered, original=reference)

        if prepared.violations:
            pass  # stay on "fallback": the violations say why
        elif plan.is_empty and rendered != reference:
            prepared.error = "round trip changed the request under an empty plan (a parse/render bug)"
        elif plan.is_empty:
            prepared.outcome = "passthrough"  # nothing changed: send the client's exact bytes
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
    if isinstance(exc, json.JSONDecodeError | RequestShapeError | StoreBusy):
        return f"{type(exc).__name__}: {exc}"  # positions and Liber's own wording, no content
    return type(exc).__name__
