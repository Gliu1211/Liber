"""When a plan takes effect: break points and clean turn boundaries (ARCHITECTURE.md §6, ADR 0004 A1).

A pending plan version becomes the applied one only at a break point, and only on a request
that ends at a clean turn boundary: its latest message is a user turn with no tool results, so
no tool loop is in flight and no mid-task reasoning is lost. Once applied, a version is used on
every request, including those in the middle of a tool loop (the plan never touches the
current turn), so the rendered history doesn't flip between versions.

The break points (§6):
    1. the user asks for it: `liber plan apply`, or undo
    2. threshold: the request fills more than `context_threshold` of the model's context window;
       recorded on every request, and applies the pending version only if
       `threshold_auto_apply` is on (v1: opt-in)
    3. idle: the conversation was idle longer than the provider's cache lifetime, so the cache
       is gone anyway and applying edits costs nothing extra; the pending version is applied
       on the next request, requested or not

Major vs minor (§6, D6): a version that only adds operations to the applied one appends after
the frozen/live boundary (minor); anything else (an undo, a rebuilt or removed summary) changes
history before it and reprocesses everything after the system prompt (major).
"""

from dataclasses import dataclass
from typing import Any

from liber.model.items import Plan
from liber.model.validate import tool_results_in
from liber.plan.versions import EMPTY, PlanState, PlanVersion

# Context windows by model id prefix (checked 2026-11-04), for the threshold break point.
# A model not listed has no threshold.
CONTEXT_WINDOWS = (
    ("claude-haiku-4-5", 200_000),
    ("claude-fable-5", 1_000_000),
    ("claude-mythos-5", 1_000_000),
    ("claude-opus-5", 1_000_000),
    ("claude-opus-4", 1_000_000),
    ("claude-sonnet-5", 1_000_000),
    ("claude-sonnet-4-6", 1_000_000),
    ("claude-haiku-5-5", 1_000_000),
)


@dataclass(frozen=True)
class PlanPolicy:
    """The tunable parts of §6 (from Settings in the app; defaults elsewhere)."""

    idle_apply_seconds: float = 300.0  # the provider's prompt-cache lifetime (5 minutes)
    context_threshold: float = 0.7  # share of the context window
    threshold_auto_apply: bool = False  # v1: crossing the threshold applies nothing unless opted in


DEFAULT_POLICY = PlanPolicy()


@dataclass(frozen=True)
class Candidate:
    """A plan version to try rendering this request with."""

    version: PlanVersion
    takes_effect: bool  # the pending version, taking effect on this request
    stale_thinking_before: int  # thinking before this message index is left out (ADR 0012)


def at_clean_turn_boundary(messages: list[Any]) -> bool:
    """Does the request end with a user turn that starts a new exchange (ADR 0004 A1)?"""
    if not messages or not isinstance(messages[-1], dict):
        return False
    return messages[-1].get("role") == "user" and not tool_results_in(messages[-1])


def context_share(model: Any, estimated_tokens: int) -> float | None:
    """The estimated share of the model's context window the request fills; None if unknown."""
    if not isinstance(model, str):
        return None
    for prefix, window in CONTEXT_WINDOWS:
        if model.startswith(prefix):
            return round(estimated_tokens / window, 4)
    return None


def pending_takes_effect(
    state: PlanState, messages: list[Any], policy: PlanPolicy, idle_seconds: float | None, share: float | None
) -> bool:
    """Is this the request where the pending version takes effect (a break point, at A1)?"""
    if state.pending is None or not at_clean_turn_boundary(messages):
        return False
    idle = idle_seconds is not None and idle_seconds > policy.idle_apply_seconds
    over_threshold = policy.threshold_auto_apply and share is not None and share >= policy.context_threshold
    return state.apply_requested or idle or over_threshold


def candidates(state: PlanState, messages: list[Any], pending_now: bool) -> list[Candidate]:
    """The versions to render this request with, best first; the empty plan is always last.

    The pending version first, if it takes effect now; then the applied one; then the empty plan,
    for a request the others don't work for (another branch, earlier history than the plan is
    about, or a plan that fails). Once any version has taken effect in the session, even the
    empty plan leaves out the thinking from before that point: it was produced against Liber's
    rendering, not the client's history (ADR 0012).
    """
    stale = state.activated_at or 0
    versions = [Candidate(state.applied, False, stale), Candidate(EMPTY, False, stale)]
    if state.pending is not None and pending_now:
        # Everything in this request was produced before the new version: all of its thinking is stale.
        versions.insert(0, Candidate(state.pending, True, len(messages)))
    unique: list[Candidate] = []
    for candidate in versions:
        if candidate.version.number not in [chosen.version.number for chosen in unique]:
            unique.append(candidate)
        if candidate.version.number == 0:
            break  # the empty plan fits every request (e.g. undo back to it): nothing after it is needed
    return unique


def is_minor(applied: Plan, pending: Plan) -> bool:
    """Does `pending` only add operations to `applied` (D6 minor compaction)?"""
    return pending.operations[: len(applied.operations)] == applied.operations
