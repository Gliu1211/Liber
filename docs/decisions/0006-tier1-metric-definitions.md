# 0006: Tier 1 metric definitions

- Status: Accepted
- Date: 2026-11-02

## Context
ARCHITECTURE.md §12 lists the Tier 1 metrics and their gates in one line each. A gate needs exact
definitions: what is counted, over which requests, and how much movement is tolerated. The
baseline (`evals/baseline.json`) is recorded by the owner, maybe on a different machine than
CI. That matters for the one timing metric.

## Decisions

**Population.** Every session fixture in `tests/fixtures/sessions/` is replayed as the requests
a client would send:
- one request per turn that ends with the user speaking (or with a system message right after
  the user);
- then the fixture's branches: a retry, an edited prompt (fork), and the client's own
  compaction.

With `"cache_marker": "last"`, the marker moves to the newest block on every request, as Claude
Code does. Every metric is computed over all replayed requests (78 from 6 sessions today).

| Metric | Definition | Better | Tolerance |
|---|---|---|---|
| `validity_rate` | Share of requests whose rendered form (with the plan the request path applies) passes `validate(rendered, original=<separately decoded copy>)` | higher | 0 |
| `round_trip_identity` | Share of requests where rendering with the **empty** plan gives the client's request back, equal as data *and* in key order. Always measured with the empty plan, also once real plans exist. | higher | 0 |
| `fallback_rate` | Share of requests whose `pipeline.prepare` outcome is `fallback` (fresh database per run) | lower | 0 |
| `token_reduction` | `1 - Σ tokens sent / Σ tokens received` over all requests, using the local token estimates (D9). 0 until plans exist. | higher | 0.02 |
| `prefix_stability` | Placeholder 1.0. Session 4 defines it on real compactions (bytes up to the frozen/live boundary unchanged after a minor compaction). | higher | 0 |
| `render_determinism` | Share of requests where two independent parse → render → `encode_request` runs give identical bytes (ADR 0004 A2) | higher | 0 |
| `p95_overhead_ms` | Nearest-rank p95 of `pipeline.prepare`'s `overhead_ms` (§3 steps 2-5), after one warm-up request | lower | **budget − value** |

**Why the p95 tolerance is "budget left."** The gate fails when a metric moves in the worse
direction by more than the baseline's tolerance. With tolerance = `10 ms − p95_at_baseline`,
the gate fails exactly when p95 exceeds the 10 ms budget (§3), whatever machine recorded the
baseline. A fixed tolerance would instead tie the threshold to how fast the owner's laptop was
that day. Change the budget with `--budget-ms` (and a baseline PR).

**Determinism.** Results and traces are written with sorted keys, and sessions are replayed in
file-name order. Random ids (session/branch uuids) are left out of the trace. Two runs on the
same code produce the same files apart from `p95_overhead_ms` (value and tolerance) and each
trace record's `overhead_ms`.

## Consequences
- The gate is an absolute latency check. If CI hardware can't meet 10 ms, the gate fails on CI;
  that's intended, because it means the budget isn't met somewhere real.
- Today's p95 on the full replay is about 5.6–8.9 ms locally, so the margin is thin. Most of the
  cost is `parse()` and identification on the 101-message session, and both scale with request
  size. Real Claude Code contexts are larger than the fixtures. Making the request path
  incremental (reusing per-message hashes from the previous request) is the likely next
  step, tracked as a follow-up.
- New metrics (Session 4: real `token_reduction` and `prefix_stability`) change the baseline. The
  owner updates it in its own PR (§12).
