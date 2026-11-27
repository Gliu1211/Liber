# 0019 — Show how full the context window is, as sent and without Liber

- Status: Proposed
- Date: 2026-11-27
- Extends: [ADR 0014](0014-dashboard.md) (the dashboard)

## Context
The owner asked for every chat to show how much of the context window is left, as Claude Code's
`/context` does, and how much it is after Liber's reduction. After the first real summary
(2026-11-19), checking that it worked took a database query: the API's count fell from about
101k to 69k tokens per request, but the dashboard only charted cache reads and writes.

What Liber knows:
- **The API's count** of each request's input: uncached + cache reads + cache writes. It's exact,
  and for a request Liber edited it is the reduced size.
- **The model's window**, from the table in `plan/policy.py` (`CONTEXT_WINDOWS`, checked
  2026-11-04).
- **Estimates of the messages** (D9): item estimates, scaled to the worker's exact counts where it
  has them, and the applied plan. `plan/cost.rendered_tokens` already estimates a request's
  message tokens under a plan, for the §6 preview.

What it doesn't know: the system prompt and tools apart (only the messages are estimated), the real
size of thinking (its estimate includes the signature; plan sizes leave it out, owner's choice
2026-11-09), and Claude Code's own reserve before it compacts (its autocompact buffer).

## Decision
A context meter for the latest request with usage, on the session list (one line) and the
session page (a line, a grid and a legend, laid out like `/context`).

1. **Used** is the API's count: exact. **Left** is the window minus used.
2. **Without Liber** is used plus the saving. The saving is estimated as in the §6 preview: message
   tokens without the plan minus with it, thinking on neither side, so the real saving is at least
   this.
   - A request sent unchanged (`passthrough`, or a `fallback`, whose messages are the client's)
     saved nothing.
   - A request sent with a version other than the applied one (an undo since): unknown, and the page
     says so.
3. **The breakdown** (session page) of what was sent:
   - Messages: kept items, estimated.
   - Liber's summaries: frozen blocks, estimated.
   - "System prompt, tools and thinking": the rest of the API's count. Liber doesn't estimate these
     apart.
   - Freed by Liber: the saving, drawn as outlined squares at the start of the free space.
   - Free space: window minus used.
   - Estimates are marked `~`.
4. **The grid** follows `/context`: 10 × 10 squares for a 200k window, 20 × 10 for a 1M one. Square
   boundaries come from rounding each running total, so the squares always add up to the grid. A
   model not in the table gets the totals and no grid.
5. **The chart** gets a dot per request for what Liber did (`rendered`, `passthrough`, `fallback`),
   so the database query isn't needed to check a plan took effect.
6. Read-only and off the request path: no stored data changes. The session list reads each
   session's newest request in one query, and loads a session's items only when that request was
   edited (to estimate its saving).

## Options considered
- **Estimate the system prompt and tools from the recorded body.** Splits the grey part as
  `/context` does, but needs bodies recorded (`LIBER_RECORD_BODIES`), and is still an estimate.
  Left for later.
- **A "without Liber" line on the usage chart** for every past request. Past requests would need
  the message estimate as sent, which isn't stored. Doing it right means recording it on the
  request path, a bigger change than this one.
- **Use `requests.context_share`** (the threshold's estimate). It counts messages only, so it
  undercounts by the whole system prompt and tools (about 35k tokens in Claude Code).

## Consequences
- Each chat shows how full its window is and how much Liber freed, without a database query.
- The figures mix one exact number (used) with estimates (the saving, the breakdown). The page says
  which is which.
- If a client runs a model with a smaller window than the table's, "left" is too high. The table
  needs keeping up to date as models change.
- Claude Code's own meter may track the client's full conversation, not what Liber sent. Not
  checked, so the two may differ.
