"""When a plan takes effect: break points and clean turn boundaries (ARCHITECTURE.md §6, ADR 0004 A1).

A pending plan version becomes the applied one only at a break point, and only on a request
that ends at a clean turn boundary: its latest message is a user turn with no tool results, so
no tool loop is in flight and no mid-task reasoning is lost. Once applied, a version is used on
every request, including those in the middle of a tool loop (the plan never touches the
current turn), so the rendered history doesn't flip between versions.

Session 4a implements break point 1 (the user asks for it: `liber plan apply` or undo).
Idle and threshold break points, and major vs minor compaction, come with cost.py in 4b.
"""

from typing import Any

from liber.model.validate import tool_results_in
from liber.plan.versions import EMPTY, PlanState, PlanVersion


def at_clean_turn_boundary(messages: list[Any]) -> bool:
    """Does the request end with a user turn that starts a new exchange (ADR 0004 A1)?"""
    if not messages or not isinstance(messages[-1], dict):
        return False
    return messages[-1].get("role") == "user" and not tool_results_in(messages[-1])


def versions_to_try(state: PlanState, messages: list[Any]) -> list[PlanVersion]:
    """The plan versions to render this request with, best first.

    A requested pending version first, if the request is at a clean turn boundary; then the
    applied one; then the empty plan, for a request the applied plan doesn't fit (another
    branch, or earlier history than the plan is about).
    """
    candidates = [state.applied, EMPTY]
    if state.pending is not None and state.apply_requested and at_clean_turn_boundary(messages):
        candidates.insert(0, state.pending)
    unique: list[PlanVersion] = []
    for version in candidates:
        if version.number not in [chosen.number for chosen in unique]:
            unique.append(version)
    return unique
