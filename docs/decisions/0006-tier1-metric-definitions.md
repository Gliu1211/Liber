# 0006: Tier 1 metric definitions

- Status: Accepted. The budget *value* (10 ms below) is superseded by ADR 0011: 20 ms, defined in `tier1.OVERHEAD_BUDGET_MS`. The mechanism is unchanged.
- Date: 2026-11-02

## Context
ARCHITECTURE.md §12 lists the Tier 1 metrics and their gates in one line each. A gate needs exact
definitions: what is counted, over which requests, and how much movement is tolerated. The
baseline (`evals/baseline.json`) is recorded by the owner, maybe on a different machine than
CI. That matters for the one timing metric.

## Decisions

**Population.** Every session fixture in `tests/fixtures/sessions/` is replayed as the requests
a client would send, as compact JSON (what Claude Code's TypeScript SDK sends):
- one request per turn that ends with the user speaking, or with a system message right after
  the user. A user message followed directly by a system message went out together with it, so
  it isn't a request on its own;
- then the fixture's branches (a retry, an edited prompt, the client's own compaction), each
  replayed turn by turn from its first new request, so continuing a fork is exercised too.

With `"cache_marker": "last"`, the marker moves to the newest block on every request, as Claude
Code does. Every metric is computed over all replayed requests (79 from 6 sessions today). A run
with no fixtures, or a session that replays to no requests, is an error (exit code 2), never a
perfect score.

| Metric | Definition | Better | Tolerance |
|---|---|---|---|
| `validity_rate` | Share of requests whose rendering **on the request path** (`pipeline.prepare`) exists and passes `validate(rendered, original=<separately decoded copy>)` | higher | 0 |
| `round_trip_identity` | Share of requests where rendering with the **empty** plan gives the client's request back, equal as data *and* in key order. Always measured with the empty plan, also once real plans exist. | higher | 0 |
| `fallback_rate` | Share of requests whose `pipeline.prepare` outcome is `fallback` (fresh database per run) | lower | 0 |
| `token_reduction` | `1 - Σ tokens sent / Σ tokens received` over all requests, using the local token estimates (D9). "Sent" is what actually went upstream: the client's own request unless the outcome is `rendered`. A render that failed validation saves nothing. 0 until plans exist. | higher | 0.02 |
| `prefix_stability` | Placeholder 1.0. Session 4 defines it on real compactions (bytes up to the frozen/live boundary unchanged after a minor compaction). | higher | 0 |
| `render_determinism` | Share of requests where two independent parse → render → `encode_request` runs give identical bytes (ADR 0004 A2) | higher | 0 |
| `p95_overhead_ms` | Nearest-rank p95 of `pipeline.prepare`'s `overhead_ms` (§3 steps 2-5), after one warm-up request. The request path runs **3 times**, each on a fresh database, and the reported value is the **median of the three p95s** | lower | **budget − value** (may be negative) |

**Why the p95 tolerance is "budget left."** The gate fails when a metric moves in the worse
direction by more than the baseline's tolerance. With tolerance = `10 ms − p95_at_baseline`,
the gate fails exactly when p95 exceeds the 10 ms budget (§3), whatever machine recorded the
baseline. A fixed tolerance would instead tie the threshold to how fast the owner's laptop was
that day. The tolerance isn't clamped at 0: if the baseline itself was over budget, it is
negative, and the gate still fails anything over budget instead of accepting the slow baseline
(tested). Change the budget with `--budget-ms` (and a baseline PR).

**Why the median of three runs.** With ~80 requests, p95 is decided by the 4th-slowest single
request, so one scheduler hiccup moved it by 2-3 ms between runs. The median of three p95s keeps
real slowdowns visible and drops one-off noise. `summary.max_overhead_ms` reports the slowest
single request (not gated), because p95 hides it.

**Determinism.** Results and traces are written with sorted keys, and sessions are replayed in
file-name order. Random ids (session/branch uuids) are left out of the trace. Each trace record
carries `rendered_sha256`, a fingerprint of the bytes Liber would send. The render_determinism
metric compares two renders inside one process, which can't see output that depends on Python's
per-process hash seed. That's the realistic A2 failure: Liber restarting between two requests.
`tests/test_evals_tier1.py` therefore runs `python -m liber.evals.tier1` twice, with different
`PYTHONHASHSEED`s, and requires identical files apart from timings (`p95_overhead_ms`,
`summary.max_overhead_ms`, each record's `overhead_ms`).

## Consequences
- The gate is an absolute latency check. If CI hardware can't meet 10 ms, the gate fails on CI;
  that's intended, because it means the budget isn't met somewhere real.
- Today's p95 on the full replay is about 6-8 ms locally, so the margin is thin. Most of the cost
  is `parse()` and identification on the 101-message session, and both scale with request size.
  Real Claude Code contexts are larger than the fixtures.
- One request is reliably slow: SQLite's automatic WAL checkpoint occasionally lands on the request
  path (~10 ms on a tiny request; it disappears with `wal_autocheckpoint=0`). p95 hides it;
  `max_overhead_ms` shows it.
- Follow-up (queued as a separate task): make the request path incremental (reuse per-message
  hashes from the previous request) and move WAL checkpoints off the request path.
- New metrics (Session 4: real `token_reduction` and `prefix_stability`) change the baseline. The
  owner updates it in its own PR (§12).
