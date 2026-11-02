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

import structlog

from liber.model.anthropic import parse, render
from liber.model.items import Plan
from liber.model.validate import Violation, validate
from liber.session.chain import identify
from liber.session.store import Store

log = structlog.get_logger(__name__)


@dataclass
class Prepared:
    """What to send upstream, and what Liber learned and did on the way (recorded per request)."""

    body: bytes
    outcome: str = "fallback"  # "passthrough", "rendered" or "fallback"
    session_id: str | None = None
    branch_id: str | None = None
    known_messages: int | None = None
    new_messages: int | None = None
    error: str | None = None  # why it fell back (an exception or a round-trip mismatch)
    violations: list[Violation] = field(default_factory=list)
    overhead_ms: float | None = None


async def prepare(store: Store, raw_body: bytes) -> Prepared:
    """Run §3 steps 2-5 on a request body. Never raises; falls back to `raw_body` on any problem."""
    started = time.perf_counter()
    prepared = Prepared(body=raw_body)
    try:
        request = json.loads(raw_body)
        parsed = parse(request)
        identity = await identify(store, parsed)
        prepared.session_id, prepared.branch_id = identity.session_id, identity.branch_id
        prepared.known_messages, prepared.new_messages = identity.known_messages, identity.new_messages

        plan = Plan()  # the session's applied plan; plans are stored from Session 4
        rendered = render(parsed, plan)
        prepared.violations = validate(rendered, original=request)

        if prepared.violations:
            pass  # stay on "fallback": the violations say why
        elif plan.is_empty and rendered != request:
            prepared.error = "round trip changed the request under an empty plan (a parse/render bug)"
        elif plan.is_empty:
            prepared.outcome = "passthrough"  # nothing changed: send the client's exact bytes
        else:
            prepared.outcome = "rendered"
            prepared.body = json.dumps(rendered, ensure_ascii=False, separators=(",", ":")).encode()
    except Exception as exc:
        # The body is the conversation, never credentials, so the message is safe to keep.
        prepared.error = f"{type(exc).__name__}: {exc}"

    prepared.overhead_ms = round((time.perf_counter() - started) * 1000, 3)
    if prepared.outcome == "fallback":
        log.warning(
            "liber_fallback",
            error=prepared.error,
            violations=[v.rule for v in prepared.violations],
            session_id=prepared.session_id,
        )
    return prepared
