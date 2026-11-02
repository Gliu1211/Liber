"""Session fixtures, and the requests a client would send while having each conversation.

ARCHITECTURE.md §12 (Tier 1 runs against recorded fixture sessions) and §13 (evals/: replay).

A client like Claude Code resends the whole conversation on every turn. So a session is stored
once, as its final request, and replayed as the sequence of requests that led up to it: one per
turn that ends with the user speaking (or with a system message right after the user).

Session fixture format (one JSON file per session in tests/fixtures/sessions/):

    {
      "description": "what this session exercises",
      "request": { ...the final Messages API request... },  # or "request_file": "relative/path.json"
      "cache_marker": "last",       # optional: move a cache_control marker to the newest block on
                                    # every request, as Claude Code does
      "branches": [                 # optional: what clients also do to history
        {"note": "...", "keep_messages": 3, "messages": [...]}
      ]                             # -> the first 3 messages of the conversation + these messages
    }

Branches cover a retry (keep N messages, add none), an edited prompt (keep the messages before
the edit, then the new ones) and the client's own compaction (keep none).
"""

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CACHE_MARKER = {"type": "ephemeral"}


@dataclass(frozen=True)
class Branch:
    note: str
    keep_messages: int  # how many messages of the main conversation it starts from
    messages: list[dict[str, Any]]  # what follows them


@dataclass(frozen=True)
class SessionFixture:
    name: str  # the file name without .json
    description: str
    request: dict[str, Any]  # the conversation's final request
    cache_marker: str | None = None
    branches: list[Branch] = field(default_factory=list)


@dataclass(frozen=True)
class ReplayedRequest:
    session: str
    index: int  # position in the session's replay, from 0
    label: str  # "turn 5" (the request carries the first 5 messages) or "branch 2"
    body: bytes  # exactly what the client would send


def load_sessions(directory: Path) -> list[SessionFixture]:
    """Every *.json session fixture in `directory`, sorted by name so runs are repeatable."""
    sessions = []
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "request_file" in data:
            request = json.loads((path.parent / data["request_file"]).read_text(encoding="utf-8"))
        else:
            request = data["request"]
        branches = [Branch(b.get("note", ""), b["keep_messages"], b["messages"]) for b in data.get("branches", [])]
        sessions.append(
            SessionFixture(
                name=path.stem,
                description=data.get("description", ""),
                request=request,
                cache_marker=data.get("cache_marker"),
                branches=branches,
            )
        )
    return sessions


def ends_a_turn(messages: list[dict[str, Any]], index: int) -> bool:
    """Would a client send a request whose last message is messages[index]?

    Yes after a user message, and after a system message that directly follows one (the API
    allows a system message as the last entry). Not after an assistant message: that is the
    model's own reply.
    """
    role = messages[index]["role"]
    if role == "user":
        return True
    return role == "system" and index > 0 and messages[index - 1]["role"] == "user"


def replay(session: SessionFixture) -> list[ReplayedRequest]:
    """The requests of `session`, in the order a client would send them."""
    full = session.request["messages"]
    histories: list[tuple[str, list[dict[str, Any]]]] = [
        (f"turn {index + 1}", full[: index + 1]) for index in range(len(full)) if ends_a_turn(full, index)
    ]
    for number, branch in enumerate(session.branches, start=1):
        histories.append((f"branch {number}", full[: branch.keep_messages] + branch.messages))

    requests = []
    for index, (label, messages) in enumerate(histories):
        if session.cache_marker == "last":
            messages = with_marker_on_last_block(messages)
        body = {**session.request, "messages": messages}
        requests.append(ReplayedRequest(session.name, index, label, encode_like_a_client(body)))
    return requests


def with_marker_on_last_block(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A copy of `messages` with one cache_control marker, on the newest content block only.

    That's how Claude Code marks its requests: the marker moves forward every turn, which must
    not confuse Liber's conversation identity (D7 ignores markers).
    """
    moved = copy.deepcopy(messages)
    for message in moved:
        if isinstance(message["content"], list):
            for block in message["content"]:
                block.pop("cache_control", None)
    last = moved[-1]
    if isinstance(last["content"], str):
        last["content"] = [{"type": "text", "text": last["content"]}]
    last["content"][-1]["cache_control"] = dict(CACHE_MARKER)
    return moved


def encode_like_a_client(body: dict[str, Any]) -> bytes:
    """JSON as the Anthropic SDKs send it (Python's default separators, UTF-8)."""
    return json.dumps(body, ensure_ascii=False).encode("utf-8")
