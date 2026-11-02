"""A Hypothesis strategy that generates Messages-API-shaped requests for property tests.

The requests are *structurally* plausible (messages with roles and string or block content,
blocks of every kind Liber distinguishes, cache_control markers, extra fields), not necessarily
valid for the API: parse/render must round-trip anything shaped like a request.
"""

from typing import Any

from hypothesis import strategies as st

json_scalars = st.none() | st.booleans() | st.integers(-(10**6), 10**6) | st.text(max_size=12)
json_values = st.recursive(
    json_scalars,
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3),
    max_leaves=8,
)
short_text = st.text(max_size=24)
tool_ids = st.sampled_from(["toolu_a", "toolu_b", "toolu_c"])

text_block = st.builds(lambda t: {"type": "text", "text": t}, short_text)
tool_use_block = st.builds(
    lambda i, inp: {"type": "tool_use", "id": i, "name": "bash", "input": inp},
    tool_ids,
    st.dictionaries(st.text(max_size=6), json_values, max_size=3),
)
tool_result_block = st.builds(
    lambda i, c: {"type": "tool_result", "tool_use_id": i, "content": c},
    tool_ids,
    short_text | st.lists(text_block, max_size=2),
)
thinking_block = st.builds(lambda t, s: {"type": "thinking", "thinking": t, "signature": s}, short_text, short_text)
redacted_block = st.builds(lambda d: {"type": "redacted_thinking", "data": d}, short_text)
image_block = st.just({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0K"}})
unknown_block = st.dictionaries(st.text(max_size=6), json_values, max_size=3).map(
    lambda fields: {**fields, "type": "some_future_block"}
)


def with_optional_cache_control(block: dict[str, Any], marked: bool) -> dict[str, Any]:
    return {**block, "cache_control": {"type": "ephemeral"}} if marked else block


blocks = st.builds(
    with_optional_cache_control,
    st.one_of(
        text_block, tool_use_block, tool_result_block, thinking_block, redacted_block, image_block, unknown_block
    ),
    st.booleans(),
)

messages = st.builds(
    lambda role, content, extra: {"role": role, "content": content, **extra},
    st.sampled_from(["user", "assistant", "system"]),
    short_text | st.lists(blocks, max_size=4),
    st.dictionaries(st.sampled_from(["clear_at", "output_config"]), json_values, max_size=1),
)

tool_definitions = st.builds(
    lambda name, marked: with_optional_cache_control(
        {"name": name, "description": "d", "input_schema": {"type": "object"}}, marked
    ),
    st.text(min_size=1, max_size=8),
    st.booleans(),
)

requests = st.fixed_dictionaries(
    {
        "model": st.just("claude-opus-5-5"),
        "max_tokens": st.integers(1, 64_000),
        "messages": st.lists(messages, max_size=8),
    },
    optional={
        "system": short_text | st.lists(st.builds(with_optional_cache_control, text_block, st.booleans()), max_size=2),
        "tools": st.lists(tool_definitions, max_size=2),
        "stream": st.booleans(),
        "metadata": st.dictionaries(st.text(max_size=6), json_values, max_size=2),
        "thinking": st.just({"type": "adaptive"}),
    },
)
