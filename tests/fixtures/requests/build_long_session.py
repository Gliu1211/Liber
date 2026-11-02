"""Write `long_session_101.json`: a synthetic 101-message Claude Code-style request.

Run from the repository root:  uv run python tests/fixtures/requests/build_long_session.py

The output is deterministic (no randomness, no timestamps), so re-running it gives the same
bytes. Shape: 10 exchanges, each "user asks -> 4 tool rounds -> assistant answers", then a final
user message, so the request ends the way a real one does (with a user turn). Tool results carry
~1.5 KB of code-like text, assistant turns carry thinking blocks with signatures, and the
system prompt, last tool and last message carry cache_control markers, as Claude Code's do.
"""

import hashlib
import json
from pathlib import Path

OUT = Path(__file__).with_name("long_session_101.json")


def fake_signature(n: int) -> str:
    return "Eq" + hashlib.sha256(f"signature-{n}".encode()).hexdigest()


def fake_file(n: int) -> str:
    lines = [f"def function_{n}_{i}(value):  # line {i}\n    return value * {i} + {n}\n" for i in range(24)]
    return "".join(lines)


def build() -> dict[str, object]:
    messages: list[dict[str, object]] = []
    tool_number = 0
    for exchange in range(10):
        messages.append(
            {"role": "user", "content": f"Task {exchange}: refactor module_{exchange}.py and run its tests."}
        )
        for round_number in range(4):
            tool_number += 1
            tool_id = f"toolu_long_{tool_number:03d}"
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": f"Exchange {exchange}, round {round_number}: inspect the next file.",
                            "signature": fake_signature(tool_number),
                        },
                        {"type": "text", "text": f"Reading file {tool_number}."},
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "read_file",
                            "input": {"path": f"src/module_{exchange}/part_{round_number}.py"},
                        },
                    ],
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": fake_file(tool_number)}],
                }
            )
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": f"Done with task {exchange}: all tests pass."}],
            }
        )
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Great. Now summarise every change.", "cache_control": {"type": "ephemeral"}}
            ],
        }
    )
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 32000,
        "stream": True,
        "thinking": {"type": "adaptive"},
        "system": [
            {
                "type": "text",
                "text": "You are a coding agent working in a Python repository.",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "tools": [
            {
                "name": "read_file",
                "description": "Read a file from the repository.",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": messages,
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
