"""Keeping thinking valid in the requests Liber edits (ADR 0004, docs/decisions/0012).

On current models a thinking block is only valid in the conversation that produced it: if
anything before it changed, the API rejects the request (on enforced accounts) unless it opts
into dropping such blocks. Liber edits history, so for every request it changes:

1. **Stale thinking is left out** (plan/apply.py): blocks from before the plan version took effect
   were produced against another history. They are the oldest blocks still in the request, a
   leading run, so leaving them out keeps every later block valid.
2. **The opt-in is added as a safety net** (`bind_thinking`): `thinking.block_binding.
   prefix_mismatch_behavior: "drop_block"` and the `thinking-binding-controls-2026-08-01` beta,
   merged into the client's `anthropic-beta` header. If step 1 is right, nothing is dropped; if
   not, the API drops the block (and reports it in `input_transformations`) instead of failing.

ADR 0004 A3's edge cases decide when the opt-in can't be added, and whether the edit is then
still sent. Checked against the API docs on 2026-11-04: `block_binding` is accepted with
thinking type "adaptive" or "enabled"; Claude Sonnet 5.5's "between_tools" takes no other field;
sending `block_binding` without the beta header is a 400.
"""

from dataclasses import dataclass
from typing import Any

from liber.model.validate import THINKING_TYPES

BINDING_BETA = "thinking-binding-controls-2026-08-01"

# Models on which leaving out `thinking` runs adaptive thinking (so adding an explicit
# {"type": "adaptive"} changes nothing about how the model thinks). Prefixes of model ids.
ADAPTIVE_BY_DEFAULT = ("claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-5-5")
# Models whose handling of thinking in edited history the docs confirm. On any other model an
# edited request that still has thinking blocks is sent unedited (ADR 0004 A3: unknown model).
KNOWN_THINKING_MODELS = (
    *ADAPTIVE_BY_DEFAULT,
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
)


@dataclass(frozen=True)
class Binding:
    """What to send for an edited request."""

    request: dict[str, Any] | None  # the request to send; None: send the client's own instead
    betas: tuple[str, ...] = ()  # values to merge into the anthropic-beta header
    reason: str | None = None  # why the edit isn't sent, when request is None


def bind_thinking(rendered: dict[str, Any]) -> Binding:
    """The edited request with its thinking protected, or why it must not be sent edited."""
    thinking = rendered.get("thinking")
    model = rendered.get("model")
    binding = thinking.get("block_binding") if isinstance(thinking, dict) else None

    if isinstance(binding, dict):
        # The client chose already; respect it (some tooling spells it `mismatch_behavior`).
        behavior = binding.get("prefix_mismatch_behavior", binding.get("mismatch_behavior"))
        if behavior == "drop_block":
            return Binding(rendered, (BINDING_BETA,))
        return Binding(None, reason=f'not sent edited: the client set prefix_mismatch_behavior "{behavior}"')

    if not (isinstance(model, str) and model.startswith(KNOWN_THINKING_MODELS)):
        if has_thinking(rendered):
            return Binding(None, reason="not sent edited: thinking in edited history isn't confirmed for this model")
        return Binding(rendered)  # no thinking block in it: nothing to keep valid, no field to risk

    opted_in = with_drop_block(rendered, model, thinking)
    if opted_in is not None:
        return Binding(opted_in, (BINDING_BETA,))
    # Thinking set some other way ("between_tools" takes no other field, older models without
    # adaptive thinking): no opt-in possible. Stale thinking is already left out, so the blocks
    # that remain were produced against this same history (ADR 0004 A3's "strip" path).
    return Binding(rendered)


def fallback_opt_in(request: Any) -> Binding | None:
    """For a fallback in a session Liber has already edited (CLAUDE.md hard rule 1, ADR 0012).

    The client's own request is sent, messages unchanged, but its thinking may have been produced
    against Liber's rendering, which an enforced account would reject. So it gets the drop_block
    opt-in (the API then drops such blocks instead of failing), when the model is confirmed, the
    client hasn't chosen a `prefix_mismatch_behavior` itself, and the API accepts the field for its
    thinking setting. None: send the client's bytes as they are.
    """
    if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
        return None
    thinking = request.get("thinking")
    if isinstance(thinking, dict) and "block_binding" in thinking:
        return None  # the client's own choice
    model = request.get("model")
    if not (isinstance(model, str) and model.startswith(KNOWN_THINKING_MODELS)):
        return None
    opted_in = with_drop_block(request, model, thinking)
    return Binding(opted_in, (BINDING_BETA,)) if opted_in is not None else None


def with_drop_block(request: dict[str, Any], model: str, thinking: Any) -> dict[str, Any] | None:
    """`request` with the drop_block opt-in in `thinking`, or None where the API doesn't accept it.

    Accepted with thinking type "adaptive" or "enabled"; when `thinking` is left out, only on models
    where that already means adaptive (adding it then changes nothing about how the model thinks).
    Everything else in the request, messages included, is the same object.
    """
    drop_block = {"prefix_mismatch_behavior": "drop_block"}
    if isinstance(thinking, dict) and thinking.get("type") in ("adaptive", "enabled"):
        return {**request, "thinking": {**thinking, "block_binding": drop_block}}
    if thinking is None and model.startswith(ADAPTIVE_BY_DEFAULT):
        return {**request, "thinking": {"type": "adaptive", "block_binding": drop_block}}
    return None


def has_thinking(request: dict[str, Any]) -> bool:
    return any(
        isinstance(block, dict) and block.get("type") in THINKING_TYPES
        for message in request["messages"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
    )
