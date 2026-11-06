"""The request path between receiving and forwarding: ARCHITECTURE.md §3 steps 2-5.

For every POST /v1/messages, `prepare()` decides which bytes go upstream:

    1. parse the JSON into items, reusing the    (model/anthropic.py)
       work done for this conversation's previous
       request (RecentRequests, ADR 0007)
    2. identify the conversation and register
       the new messages and items                (session/chain.py)
    3. render with the session's plan: a pending  (plan/policy.py, plan/versions.py,
       version takes effect at a break point       plan/apply.py)
       (asked for, idle, threshold) on a clean
       turn, else the applied one; a version that
       doesn't fit is skipped (edit_skipped says why)
    4. validate the rendered request             (model/validate.py)
    5. keep thinking valid in an edited request  (plan/thinking.py, ADR 0004, 0012)
    6. choose what to send:
         "passthrough"  nothing changed, or the  -> the client's exact bytes
                        edit can't be sent
         "rendered"     something changed        -> the rendered request, and the beta header
                                                    to merge (Prepared.betas)
         "fallback"     anything went wrong      -> the client's exact bytes, and the reason

(§3 lists identification before parsing; here parsing comes first because the chain hashes that
identify the conversation are computed from the parsed messages.)

Once a plan version has taken effect in a session, the thinking produced before that point is
stale and left out on every later request, even with the empty plan (ADR 0012). A session that
never had a plan is always sent byte-identical.

Hard rule 1: `prepare()` never raises and never returns anything but the client's bytes unless
a plan (or stale thinking) changed the request and the result passed validation. One amendment:
a fallback in a session Liber has already edited carries the drop_block opt-in, messages
unchanged (`add_fallback_opt_in`).
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from liber.model.anthropic import ParsedRequest, RecentRequests, RequestShapeError, parse, render
from liber.model.items import Item, OperationKind
from liber.model.validate import Violation, validate
from liber.plan import policy, thinking, versions
from liber.plan.apply import PlanNotApplicable
from liber.session.chain import identify
from liber.session.store import Store, StoreBusy

log = structlog.get_logger(__name__)


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
    betas: tuple[str, ...] = ()  # beta values to merge into the anthropic-beta header (edited requests)
    context_share: float | None = None  # estimated share of the model's context window (§6)
    parsed: ParsedRequest | None = None  # the client's request as items, for the background worker (§9)


async def prepare(
    store: Store,
    raw_body: bytes,
    recent: RecentRequests | None = None,
    plan_policy: policy.PlanPolicy = policy.DEFAULT_POLICY,
) -> Prepared:
    """Run §3 steps 2-5 on a request body. Never raises; falls back to `raw_body` on any problem.

    `recent`: the app's cache of recent conversations, so only new messages are hashed (ADR 0007).
    Without it every message is hashed, with the same result. `plan_policy`: §6's break points.
    """
    started = time.perf_counter()
    prepared = Prepared(body=raw_body)
    edited_session = False  # known once the conversation is identified
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
        prepared.parsed = parsed
        if recent is not None:
            recent.remember(parsed, replacing=previous)
        identity = await identify(store, parsed)
        prepared.session_id, prepared.branch_id = identity.session_id, identity.branch_id
        prepared.known_messages, prepared.new_messages = identity.known_messages, identity.new_messages

        state = identity.plan
        # Liber has edited this session before: its fallbacks need the drop_block opt-in (rule 1).
        edited_session = state.activated_at is not None or state.applied.number > 0
        messages = request["messages"]
        estimated_tokens = sum(item.tokens for item in parsed.items)
        prepared.context_share = policy.context_share(request.get("model"), estimated_tokens)
        pending_now = policy.pending_takes_effect(
            state, messages, plan_policy, identity.idle_seconds, prepared.context_share
        )
        chosen, rendered, prepared.violations = render_with_plan(
            parsed, policy.candidates(state, messages, parsed.chain_hashes, pending_now), reference, prepared
        )
        prepared.rendered = rendered
        used = chosen.version
        if used.number:
            prepared.plan_version = used.number
            prepared.offloaded = offloaded_items(parsed, used)

        if prepared.violations:
            return finish(prepared, started, raw_body, edited_session)  # "fallback": the violations say why
        untouched = used.plan.is_empty and chosen.stale_thinking_before == 0
        if untouched and rendered != reference:
            prepared.error = "round trip changed the request under an empty plan (a parse/render bug)"
            return finish(prepared, started, raw_body, edited_session)

        # An edited request keeps its thinking valid, or goes out unedited (plan/thinking.py).
        to_send: dict[str, Any] | None = None
        if rendered != reference:
            binding = thinking.bind_thinking(rendered)
            to_send, prepared.betas = binding.request, binding.betas
            prepared.edit_skipped = binding.reason or prepared.edit_skipped

        # A pending version takes effect once a request goes out as it renders it (an undo back to
        # the empty plan included); not when its edit couldn't be sent. Written before the body is
        # chosen: if the write fails, the except below still sends the client's bytes.
        sent_as_rendered = rendered == reference or to_send is not None
        activation = (len(messages), parsed.chain_hashes[-1]) if parsed.chain_hashes else None
        if chosen.takes_effect and identity.session_id and activation and sent_as_rendered:
            await versions.promote_pending(store, identity.session_id, used.number, activation)
        elif needs_activation_point(state, used, messages) and identity.session_id and activation and to_send:
            # A version applied before activation points were recorded (Session 4a): this clean
            # turn left out all earlier thinking, so it can serve as the point from now on.
            await versions.backfill_activation(store, identity.session_id, used.number, activation)

        if to_send is not None:
            prepared.outcome = "rendered"
            prepared.rendered = to_send
            prepared.body = encode_request(to_send)
        else:
            prepared.betas = ()
            prepared.outcome = "passthrough"  # the client's exact bytes
    except Exception as exc:
        # Whatever was decided before the failure: the client's own bytes and headers.
        prepared.outcome, prepared.body, prepared.betas = "fallback", raw_body, ()
        prepared.error = describe_failure(exc)
    return finish(prepared, started, raw_body, edited_session)


def finish(prepared: Prepared, started: float, raw_body: bytes, edited_session: bool) -> Prepared:
    if prepared.outcome == "fallback" and edited_session:
        add_fallback_opt_in(prepared, raw_body)
    prepared.overhead_ms = round((time.perf_counter() - started) * 1000, 3)
    if prepared.outcome == "fallback":
        log.warning(
            "liber_fallback",
            error=prepared.error,
            violations=[v.rule for v in prepared.violations],
            session_id=prepared.session_id,
        )
    return prepared


def add_fallback_opt_in(prepared: Prepared, raw_body: bytes) -> None:
    """Hard rule 1's one amendment (owner-approved; ADR 0012).

    In a session Liber has already edited, the client's history can hold thinking produced against
    Liber's rendering, which an enforced account rejects. The fallback still sends the client's
    messages unchanged, but with the drop_block opt-in (plan/thinking.fallback_opt_in), so the API
    drops such blocks instead of failing. Decoded afresh from the client's bytes: nothing Liber
    did to its own copy can leak into it. If anything goes wrong here, the bytes go as they came.
    """
    try:
        binding = thinking.fallback_opt_in(json.loads(raw_body))
        if binding is None or binding.request is None:
            return
        prepared.body, prepared.betas = encode_request(binding.request), binding.betas
        prepared.error = f"{prepared.error or 'validation failed'}; sent with the drop_block opt-in (edited session)"
    except Exception as exc:
        prepared.body, prepared.betas = raw_body, ()
        log.warning("fallback_opt_in_failed", error=type(exc).__name__)


def render_with_plan(
    parsed: ParsedRequest, candidates: list[policy.Candidate], reference: Any, prepared: Prepared
) -> tuple[policy.Candidate, dict[str, Any], list[Violation]]:
    """Render with the first candidate version (policy.candidates) that works for this request.

    A version gives way to the next one if it doesn't fit the request, if applying it fails (a
    bug: one bad plan mustn't stop the session's other versions, or an undo, from working), or
    if its render doesn't pass the validator. The last candidate is the empty plan; its
    violations or failure are the request's, and prepare() falls back.
    """
    *plans, last = candidates
    for candidate in plans:
        number = candidate.version.number
        try:
            rendered = render(parsed, candidate.version.plan, candidate.stale_thinking_before)
        except PlanNotApplicable as exc:
            prepared.edit_skipped = f"plan version {number} not used: {exc}"
            continue
        except Exception as exc:
            prepared.edit_skipped = f"plan version {number} not used: it failed ({describe_failure(exc)})"
            log.warning("plan_failed", plan_version=number, error=describe_failure(exc))
            continue
        violations = validate(rendered, original=reference)
        if violations:
            rules = ", ".join(sorted({violation.rule for violation in violations}))
            prepared.edit_skipped = f"plan version {number} not used: its render broke {rules}"
            continue
        return candidate, rendered, []
    rendered = render(parsed, last.version.plan, last.stale_thinking_before)
    return last, rendered, validate(rendered, original=reference)


def needs_activation_point(state: versions.PlanState, used: versions.PlanVersion, messages: list[Any]) -> bool:
    return (
        used.number > 0
        and used.number == state.applied.number
        and state.activated_hash is None
        and policy.at_clean_turn_boundary(messages)
    )


def offloaded_items(parsed: ParsedRequest, version: versions.PlanVersion) -> list[Item]:
    """This request's items that the plan offloads, so their originals can be stored (§4)."""
    ids = {
        item_id
        for operation in version.plan.operations
        if operation.kind is OperationKind.OFFLOAD
        for item_id in operation.item_ids
    }
    return [item for item in parsed.items if item.id in ids]


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
