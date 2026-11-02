"""Read the upstream response as it streams past, to rebuild the reply and its token usage.

ARCHITECTURE.md §3 step 7 ("record while streaming") and D9 (the `usage` numbers are the
ground truth for token totals and cache reads vs writes).

The proxy relays bytes to the client untouched; this module only *watches* a copy of them.
Three layers, each usable on its own:

- `SSEParser` turns raw bytes into server-sent events (`event:` name + `data:` text).
- `StreamAssembler` turns Messages API events into the final Message object, the same way the
  Anthropic SDKs' `get_final_message()` does.
- `ReplyCapture` picks the right approach for a response (SSE stream vs plain JSON body) and
  never raises, because a parsing problem must never break the user's session (hard rule 1).

Event shapes follow https://platform.claude.com/docs/en/build-with-claude/streaming.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

# --- Server-sent events -----------------------------------------------------------------------


@dataclass(frozen=True)
class SSEEvent:
    event: str  # the `event:` field; "message" when absent (SSE spec default)
    data: str  # all `data:` lines of the event, joined with "\n"


class SSEParser:
    """Incremental SSE parser: feed it bytes in whatever chunks they arrive.

    An event is complete only when its terminating blank line has arrived, so a chunk may yield
    zero, one, or many events, and any incomplete tail is kept for the next call.
    """

    def __init__(self) -> None:
        self._buffer = b""
        self._event_name = ""
        self._data_lines: list[str] = []

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        self._buffer += chunk
        # Split on b"\n" *before* decoding: a chunk can end in the middle of a multi-byte UTF-8
        # character, but never in the middle of a complete line.
        *lines, self._buffer = self._buffer.split(b"\n")
        events = []
        for raw_line in lines:
            line = raw_line.removesuffix(b"\r").decode("utf-8", errors="replace")
            event = self._process_line(line)
            if event is not None:
                events.append(event)
        return events

    def _process_line(self, line: str) -> SSEEvent | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None  # a comment (servers use these as keep-alives)
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")  # the spec strips exactly one leading space
        if name == "event":
            self._event_name = value
        elif name == "data":
            self._data_lines.append(value)
        # `id` and `retry` fields exist in SSE but the Messages API does not use them.
        return None

    def _dispatch(self) -> SSEEvent | None:
        event = None
        if self._data_lines:  # per the spec, an event with no data is dropped
            event = SSEEvent(event=self._event_name or "message", data="\n".join(self._data_lines))
        self._event_name = ""
        self._data_lines = []
        return event


# --- Usage ------------------------------------------------------------------------------------


class Usage(BaseModel):
    """Token counts reported by the API for one response (D9). None means "not reported"."""

    model_config = ConfigDict(extra="ignore")

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> "Usage":
        return cls.model_validate(message.get("usage") or {})


# --- Rebuilding the Message from stream events ------------------------------------------------


class StreamAssembler:
    """Applies Messages API stream events, in order, to build the final Message dict.

    message_start      -> the Message skeleton (id, model, usage with input + cache tokens)
    content_block_*    -> one content block per `index`, grown by deltas
    message_delta      -> stop_reason and the final, cumulative usage
    message_stop       -> done
    error              -> the stream failed part-way (e.g. overloaded_error)
    """

    def __init__(self) -> None:
        self.message: dict[str, Any] | None = None
        self.complete = False
        self.error: dict[str, Any] | None = None
        self.problem: str | None = None  # something odd but survivable, e.g. bad tool JSON
        # Tool inputs stream as fragments of a JSON string; parse them once the block ends.
        self._partial_json: dict[int, str] = {}

    def handle(self, event: SSEEvent) -> None:
        payload = json.loads(event.data)
        kind = payload.get("type")
        if kind == "message_start":
            self.message = payload["message"]
            self.message.setdefault("content", [])
        elif kind == "error":
            self.error = payload.get("error")
        elif kind == "message_stop":
            self.complete = True
        elif self.message is None:
            return  # nothing to attach the event to (malformed stream); ignore it
        elif kind == "content_block_start":
            self._set_block(payload["index"], payload["content_block"])
        elif kind == "content_block_delta":
            self._apply_delta(payload["index"], payload["delta"])
        elif kind == "content_block_stop":
            self._finish_block(payload["index"])
        elif kind == "message_delta":
            self._apply_message_delta(payload)
        # Anything else (`ping`, or event types added in the future) is deliberately ignored:
        # the docs say new event types may appear and clients must tolerate them.

    def _blocks(self) -> list[Any]:
        assert self.message is not None
        content: list[Any] = self.message["content"]
        return content

    def _set_block(self, index: int, block: dict[str, Any]) -> None:
        blocks = self._blocks()
        while len(blocks) <= index:
            blocks.append(None)
        blocks[index] = block

    def _apply_delta(self, index: int, delta: dict[str, Any]) -> None:
        block = self._blocks()[index]
        kind = delta.get("type")
        if kind == "text_delta":
            block["text"] = block.get("text", "") + delta["text"]
        elif kind == "input_json_delta":
            self._partial_json[index] = self._partial_json.get(index, "") + delta["partial_json"]
        elif kind == "thinking_delta":
            block["thinking"] = block.get("thinking", "") + delta["thinking"]
        elif kind == "signature_delta":
            block["signature"] = delta["signature"]  # sent whole, once, at the end of the block
        elif kind == "citations_delta":
            block.setdefault("citations", []).append(delta["citation"])
        # Unknown delta types are ignored for the same reason as unknown events.

    def _finish_block(self, index: int) -> None:
        if index not in self._partial_json:
            return
        raw = self._partial_json.pop(index)
        try:
            self._blocks()[index]["input"] = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            # Can happen when output is cut off by max_tokens mid tool call. Keep going so the
            # usage numbers are still recorded.
            self._blocks()[index]["input"] = {}
            self.problem = f"tool input for content block {index} was not valid JSON"

    def _apply_message_delta(self, payload: dict[str, Any]) -> None:
        assert self.message is not None
        self.message.update(payload.get("delta") or {})  # stop_reason, stop_sequence, ...
        # Usage in message_delta is cumulative, so it replaces earlier numbers. A field that is
        # missing or null keeps its earlier value (same rule as the SDKs).
        usage = self.message.setdefault("usage", {})
        for name, value in (payload.get("usage") or {}).items():
            if value is not None:
                usage[name] = value


# --- Capturing a whole response ---------------------------------------------------------------


@dataclass
class CapturedReply:
    """What Liber learned from one upstream response."""

    message: dict[str, Any] | None = None  # the assistant Message, if there was one
    body_text: str | None = None  # text to store as the response body (None = nothing usable)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None
    problem: str | None = None  # error type or parse problem worth recording, if any


class ReplyCapture:
    """Receives a copy of every response chunk; `result()` summarises the reply at the end.

    Never raises: on any parsing failure it stops parsing and reports the problem instead.
    """

    def __init__(self, content_type: str, content_encoding: str | None) -> None:
        self._is_stream = content_type.startswith("text/event-stream")
        # Liber asks upstream for an uncompressed body (docs/decisions/0001); if it gets a
        # compressed one anyway, the bytes are still relayed, just not parsed.
        self._encoding = content_encoding if content_encoding not in (None, "", "identity") else None
        self._parser = SSEParser()
        self._assembler = StreamAssembler()
        self._raw = bytearray()  # only used for non-streaming bodies
        self._failure: str | None = None

    def feed(self, chunk: bytes) -> None:
        if self._failure is not None or self._encoding is not None:
            return
        try:
            if self._is_stream:
                for event in self._parser.feed(chunk):
                    self._assembler.handle(event)
            else:
                self._raw += chunk
        except Exception as exc:
            self._failure = f"could not parse response stream: {type(exc).__name__}: {exc}"

    def result(self) -> CapturedReply:
        if self._encoding is not None:
            return CapturedReply(problem=f"response body is {self._encoding}-encoded; not parsed")
        if self._is_stream:
            return self._stream_result()
        return self._json_result()

    def _stream_result(self) -> CapturedReply:
        message = self._assembler.message
        reply = CapturedReply(message=message, problem=self._failure or self._assembler.problem)
        if self._assembler.error is not None:
            reply.problem = f"stream error event: {json.dumps(self._assembler.error)}"
        if message is not None:
            reply.body_text = json.dumps(message, ensure_ascii=False)
            reply.stop_reason = message.get("stop_reason")
            reply.usage = self._safe_usage(message, reply)
        return reply

    def _json_result(self) -> CapturedReply:
        text = self._raw.decode("utf-8", errors="replace")
        reply = CapturedReply(body_text=text)
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            return reply  # e.g. an HTML error page from a gateway; keep the text only
        if not isinstance(body, dict):
            return reply
        if body.get("type") == "error":
            reply.problem = f"upstream error: {json.dumps(body.get('error'))}"
        elif body.get("type") == "message":
            reply.message = body
            reply.stop_reason = body.get("stop_reason")
            reply.usage = self._safe_usage(body, reply)
        return reply

    @staticmethod
    def _safe_usage(message: dict[str, Any], reply: CapturedReply) -> Usage:
        try:
            return Usage.from_message(message)
        except ValueError as exc:  # pydantic's ValidationError is a ValueError
            reply.problem = f"unexpected usage format: {exc}"
            return Usage()
