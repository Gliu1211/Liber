# 0010: Tier 1 standard plans, and the real token_reduction and prefix_stability

- Status: Proposed
- Date: 2026-11-04

## Context
ADR 0006 defined `token_reduction` (0 until plans exist) and left `prefix_stability` as a 1.0
placeholder for Session 4 to define "on real compactions". The Session 4 prompt asks for
"standard plans per fixture" so both become real numbers.

## Decisions
**Standard plan** (`liber/evals/tier1.py:standard_plan`), built from each fixture session's
final request (item ids don't change as the conversation grows), for sessions with at least
three exchanges:
- **Version 1:** summarize the first exchange; offload the tool results of the second exchange
  that are at least 200 estimated tokens.
- **Version 2:** version 1 plus a summary of the second exchange. This is a *minor compaction*:
  it appends a frozen block.

**How plans enter the replay** (`run_request_path(requests, plans)`): as a user would make
them. After the session's first request, version 1 is created and requested. Once version 1 is
applied (an edit with it was sent), version 2 is created and requested. In sessions whose
edits are held back for thinking (ADR 0009), version 1 stays pending, so version 2 never comes. The request path applies
each at the next clean turn that it fits (ADR 0009). Branch requests whose history lacks the
plan's items use the empty plan. A branch that starts a new session (the client's own
compaction) gets no plan. Plan commands run between requests and aren't timed.

**Metrics:**
- `token_reduction` keeps ADR 0006's definition. "Sent" is what actually went upstream, so edits
  held back for thinking (ADR 0009) save nothing.
- `prefix_stability`: of the requests rendered with version 2 on the request path, the share
  where rendering with version 2 keeps version 1's bytes up to the end of its frozen block
  (system, tools and the earlier frozen block), i.e. a minor compaction only appended (D5).
  `summary.minor_compactions_checked` counts them (7 today). A run with standard plans but no
  minor compaction scores 0, which fails the gate, rather than a vacuous 1.0. Tolerance 0.
- `validity_rate`, `render_determinism` and `round_trip_identity` are unchanged in definition.
  The first two are now measured with the plan version each request was actually rendered with;
  round trip is still always the empty plan.

**Today's values:** validity 1, round trip 1, fallback 0, determinism 1, prefix_stability 1,
token_reduction 0.0075. These move the baseline (no `evals/baseline.json` exists yet), and
the owner records it in its own PR (§12).

## Consequences
- `token_reduction` is small and partly negative per session. The two plain chat fixtures send
  *more* tokens, because a stub summary is longer than a 10-token exchange. Most fixture tokens
  sit in thinking-heavy sessions whose edits are held until 4b, which should raise the number
  substantially.
- The standard plan is a test policy, not a product default. 4b's cost model decides when
  compacting pays.
- A metric test shows `prefix_stability` drops to 0 when a render inserts the new frozen block
  before the old one.
