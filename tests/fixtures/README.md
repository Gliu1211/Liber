# Test fixtures

Recorded-format Messages API responses used by the offline tests (CLAUDE.md hard rule 7).

They are **synthetic**: written to match the documented wire format
(https://platform.claude.com/docs/en/build-with-claude/streaming) rather than captured from a
live account, so they contain no credentials, real IDs, or user data. If you add a real
recording, scrub `x-api-key`, `authorization`, cookies and any personal content first.

| File | What it is |
|---|---|
| `stream_thinking_text_tool.sse` | A streamed reply with a thinking block, a ping, a text block containing multi-byte UTF-8, and a tool_use block built from `input_json_delta`s. Usage includes cache read/write tokens. |
| `stream_thinking_text_tool.expected.json` | The message the stream above must reassemble into (written by hand, independent of the parser). |
| `stream_error_midway.sse` | A stream that starts normally and then sends an `overloaded_error` event. |
| `message_nonstream.json` | A non-streaming Messages API response body. |

`.gitattributes` marks this folder `-text` so line endings are never rewritten: the proxy tests
compare these bytes exactly.
