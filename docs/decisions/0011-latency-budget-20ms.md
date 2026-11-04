# 0011: Request-path latency budget is 20 ms (p95)

- Status: Accepted (owner, 2026-11-04)
- Date: 2026-11-04

## Context
ARCHITECTURE.md §3 asked steps 2-5 to add "only a few milliseconds", and Tier 1 (ADR 0006) and
`test_request_path_overhead_p95_under_10ms` enforced that as a 10 ms p95.

After Session 4a, plans are read and applied on every request, which added ~1.5-2 ms. Measured on
a 2-vCPU container, five Tier 1 runs each: `main` before 4a gave p95 5.9-9.0 ms (median 7.4),
Session 4a gave 7.4-9.5 ms (median 9.1). In three `make check` runs of 4a, the in-suite latency
test failed once, and one eval run reached 11.9 ms. With a baseline recorded at ~9 ms, ADR 0006's
tolerance (budget - value) would leave under 1 ms of headroom, so the gate would fail at random.

## Options considered
1. **Keep 10 ms and optimize further.** ADR 0007 lists measured next steps. Costs agent time for
   a gain users can't perceive, and the margin would shrink again with every feature.
2. **Raise the budget to 20 ms (chosen).**
3. **Make timing report-only (not gated).** Loses the protection against real regressions,
   such as the ~40 ms request path at 1 MB before ADR 0007.

## Decision
The request-path p95 budget is **20 ms**, defined once as `liber.evals.tier1.OVERHEAD_BUDGET_MS`.
The in-suite test and the Tier 1 eval both use that constant, so they can't disagree.

Why 20 ms: the model takes seconds per reply, so 20 ms is under 1% of the wait. It is about twice
today's p95, so random noise doesn't fail the gate, yet a real regression (a new O(n) pass over the
whole request, a database call on the request path) still does.

## Consequences
- ADR 0006's mechanism is unchanged: the baseline stores tolerance = budget - value, so once the
  baseline is committed (a protected file), agents can't loosen the budget.
- Record the baseline **after** this change, so it carries the 20 ms budget.
- No more latency optimization unless the gate fails or a user-visible problem appears.
- The constant lives in `liber/evals/tier1.py`, which the guard hook doesn't protect. The
  committed baseline still locks the eval gate's tolerance, so loosening the constant would only
  loosen the in-suite test; reviewers should treat any change to it as an owner decision.
