# 0002: Record exchanges in background tasks; store an allowlist of headers

- Status: Accepted
- Date: 2026-11-02

## Context
ARCHITECTURE.md §3 step 7 records every `POST /v1/messages` (usage, cache reads/writes,
latency), but hard rule 1 says a Liber failure must never break the user's session, and §3 sets
a latency budget of a few milliseconds. Hard rule 2 says credentials are never stored.

## Options considered
1. Write the database row inline, after the last chunk but before the response ends.
2. Write it in a separate asyncio task once the body has ended.
3. Put records on a queue drained by a dedicated writer (or the §9 worker).

For headers: store all except a denylist of credential headers, or store only an allowlist.

## Decision
- `ReplyCapture` watches a copy of every chunk while relaying. When the body ends (or stops
  early), `relay` calls `finish`, which completes the `RequestRecord` and hands it to
  `spawn_write`: an `asyncio` task kept in `app.state.pending_writes`. Failures are logged and
  never reach the client. Shutdown (and the tests) wait for pending writes.
- Only an allowlist of request headers is stored (`anthropic-version`, `anthropic-beta`,
  `content-type`, `user-agent`), so a new or unexpected credential header can never be stored
  by accident.

## Consequences
- The client's response never waits on SQLite.
- If the process dies between the end of a response and the write, that row is lost. That's
  acceptable for metrics; revisit if records become needed for correctness (e.g. undo in §4).
- Option 3 is a natural refactor once the §9 worker exists, if write volume ever matters.
