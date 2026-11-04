# Progress log

Append-only. Every session adds an entry at the bottom before opening its PR. The next session reads this first.

Template:

```
## YYYY-MM-DD — Session N: <task>
- Done:
- Decisions (link ADRs):
- Stubbed / provisional:
- Open issues:
- make check: pass | fail (why)
- Next step:
```

---

## 2026-11-02 — Setup
- Done: ARCHITECTURE.md, CLAUDE.md, harness (.claude hooks + reviewer subagent), Makefile, eval gate script, CI workflow.
- Next step: Session 1 (scaffold + passthrough proxy).

## 2026-11-02 — Session 1: scaffold + transparent passthrough proxy
- Done: unpacked the harness from `liber.zip` (it had been committed zipped); uv project with Python 3.11 pinned and a committed `uv.lock`; package layout per §13 (future packages hold docstrings only); `liber serve` on 127.0.0.1:8484; `POST /v1/messages` and every other path forwarded with httpx and relayed chunk by chunk (raw bytes, upstream status and headers, hop-by-hop dropped, headers handled as bytes); credentials passed through, never stored or logged; `proxy/sse.py` (incremental SSE parser, reply reassembly, usage extraction); `requests` table in SQLite with usage, cache reads/writes, latency and optional bodies; README. Independent review run; all must/should findings fixed (non-ASCII headers, unguarded recording before forwarding, case-insensitive media type, private data dir, error labelling).
- Decisions: [ADR 0001](decisions/0001-relay-raw-bytes-identity-encoding.md) (ask upstream for `identity`, relay raw bytes); [ADR 0002](decisions/0002-record-in-background-header-allowlist.md) (record in background tasks, store an allowlist of headers). Ruff line length is 120 so the owner's `scripts/eval_gate.py` passes unchanged; `.claude/` is excluded from ruff (protected). Error text stored/logged is the exception type only, except network/timeout errors (protocol errors can quote header values). Tests use `httpx.MockTransport` instead of respx.
- Stubbed / provisional: only `POST /v1/messages` is recorded; request bodies are read fully into memory before forwarding (fine for Messages API, not for huge `/v1/files` uploads); no `liber purge` yet (§11); `create_all` instead of Alembic (§8); `liber/cli.py` and `liber/log.py` are additions to the §13 layout.
- Open issues: disconnect detection relies on uvicorn reporting ASGI spec 2.3 (Starlette then listens for disconnects); with ≥ 2.4 a disconnect is noticed on the next chunk (pings keep that short) — `test_client_disconnect_releases_the_upstream_and_is_recorded` would catch a change. SSE parser ignores lone-CR line endings and a BOM (Anthropic sends neither).
- make check: pass (59 tests; eval gate skips until Session 3)
- Next step: Session 2 — context model, validator, session chain. The request path in `liber/app.py:proxy_messages` is where identify → parse → render → validate slots in, between reading the body and `open_upstream`; on any failure it must forward the original `body`.

## 2026-11-02 — Session 2: context model, validator, session chain
- Done: `model/items.py` (Item, Operation, Plan); `model/anthropic.py` parse/render (empty plan reproduces every request exactly, incl. key order; Hypothesis-tested); `model/validate.py` (§5 rules + current-API system-message placement; thinking and in-flight tool loop checked against an independently decoded copy of the client's request; thinking may only be dropped from the front); `session/chain.py` (canonicalization, hash chain, longest-prefix lookup, branches, item registration); new tables `sessions`, `branches`, `chain_nodes`, `items`; `liber/pipeline.py` wired into `POST /v1/messages` (outcome, identity, violations and overhead recorded per request). Independent review run; all should-fix findings fixed (busy-database stalls, BEGIN IMMEDIATE, order-aware thinking check, in-place-edit blind spot, whitespace text, duplicate tool ids, chain edge-case tests, error text that could quote the request).
- Decisions: [ADR 0003](decisions/0003-sqlite-in-worker-threads.md) (sync SQLAlchemy engine in worker threads, WAL; p95 went 18 ms → ~6 ms); [ADR 0004](decisions/0004-preserved-thinking-and-history-edits.md) (**Proposed — owner must decide before Session 4**: preserved thinking rejects edited history on current models; recommendation is `drop_block` on edited requests); [ADR 0005](decisions/0005-conversation-identity-details.md) (what is hashed, sessions vs branches, item ids, concurrency). Owner accepted ADRs 0001/0002.
- Stubbed / provisional: the applied plan is always empty and `render()` raises `UnsupportedPlan` for operations (Session 4); token counts are local estimates (Session 5); `liber/pipeline.py` and the `branches` table are additions to §13/§8; D7 caveat: a mid-conversation change to Claude Code's system prompt or tools starts a new session (ADR 0005 lists options).
- Open issues: if the database is busy (another writer holds SQLite's lock), identification gives up after ≤ ~0.35 s and the request goes through untouched (`liber_error` "StoreBusy"); request-path p95 is ~5.5–6.8 ms locally vs the 10 ms gate — watch CI timing. `add_missing_columns` refuses to add a NOT NULL column (needs a real migration).
- make check: pass (153 tests)
- Next step: Session 3 — Tier 1 eval harness. The pipeline's `Prepared` (outcome, violations, overhead_ms) and the request fixtures in `tests/fixtures/requests/` are ready to feed validity_rate, round_trip_identity, fallback_rate and p95_overhead_ms. Before Session 4: decide ADR 0004.

## 2026-11-02 — Owner decision: preserved thinking (ADR 0004)
- Done: ADR 0004 accepted with amendments A1–A5; ARCHITECTURE.md §5, D8, §6, §12 and SESSION_PROMPTS.md (Sessions 3, 4, 5, 7) updated to match.
- Decisions: edited requests send `drop_block`; plans apply only at clean turn boundaries; rendering must be byte-identical (new Tier 1 metric `render_determinism`); `input_transformations` recorded; `carry_forward_reasoning` summarizer flag (default off) tested in a four-condition Tier 2 ablation.
- Next step: merge PR #2, then Session 3 (Tier 1 evals, now including render_determinism).

## 2026-11-02 — Session 3: Tier 1 eval harness and gate
- Done: `liber/evals/replay.py` (session fixtures → the requests a client sends: compact JSON, one per user turn, system-message turns, retry/fork/compaction branches replayed turn by turn, Claude Code-style moving cache marker); `liber/evals/metrics.py` (metric records, nearest-rank p95, results/trace writers in the `scripts/eval_gate.py` format); `liber/evals/tier1.py` (`python -m liber.evals.tier1 --out …`, 7 metrics, per-request trace with plan version, validator result, tokens before/after, rendered_sha256); `pipeline.encode_request` and `Prepared.rendered`; 6 session fixtures (79 requests). Independent review run; all findings fixed (over-budget baseline loophole, empty runs, cross-process determinism, token_reduction counting unsent renders, p95 noise, replay realism, validity measured on the request path's own render).
- Decisions: [ADR 0006](decisions/0006-tier1-metric-definitions.md): metric definitions; p95 tolerance = budget − value (may be negative) so the gate enforces the absolute 10 ms budget on any machine; p95 = median of 3 runs; cross-process determinism checked by running the CLI under two hash seeds.
- Stubbed / provisional: `token_reduction` is 0 and `prefix_stability` a 1.0 placeholder until Session 4. No `evals/baseline.json` yet: the gate only warns until the owner records it (owner-only, its own PR).
- Open issues: p95 ≈ 6–8 ms locally vs the 10 ms budget (thin margin; cost scales with request size); SQLite's automatic WAL checkpoint occasionally lands on a request (10–28 ms; `summary.max_overhead_ms`). Both are in a queued follow-up task (incremental request path + background checkpoints).
- make check: pass
- Next step: owner records the Tier 1 baseline in its own PR. Then Session 4 (plans, frozen blocks, break points), which must extend the harness with standard plans so `token_reduction` and `prefix_stability` become real, and keep every Tier 1 metric green.

## 2026-11-02 — Request path: hash only new messages; WAL checkpoints in the background
- Done: `parse(request, previous)` reuses the previous request's chain hash, block hashes and token estimates for every message that is the same message after the same history (`==` in C; a type-checking `chain.same_json` for messages with numbers/booleans, which Python's `==` conflates), so the result is exactly a fresh parse (Hypothesis: arbitrary pairs, "next request" pairs, re-spelled history). `RecentRequests` (latest request of the last 8 conversations, in `app.state`) chooses `previous`; `pipeline.prepare(store, body, recent)`. SQLite auto-checkpoint off on every connection; `async with checkpointing(store, seconds)` runs `PRAGMA wal_checkpoint(PASSIVE)` in the background for the app's lifetime (`LIBER_WAL_CHECKPOINT_SECONDS`, default 1 s) and waits for an in-flight checkpoint before the store closes. The Tier 1 harness sets up the request path the same way. `scripts/bench_request_path.py` (Tier 1 p95/max per run + a ~1 MB, 401-message session). Independent review run: no must-fix; should-fix items fixed (recursion limit on deep JSON, interval validation, stronger property tests, clean task shutdown, harness independent of `LIBER_*` env).
- Measured (before = Session 3 commit, same machine/inputs, back to back): Tier 1 `p95_overhead_ms` median of 5 `make eval` runs 6.35 → 5.41 ms; bench Tier 1 p95 6.69 → 5.38 ms, max per run 13.4 → 6.5 ms (median of 10); ~1 MB request p95 35-46 → 19-26 ms; `parse()` 2.8 → 0.85 ms (101 messages), 14.7 → 3.5 ms (1 MB). All other Tier 1 metrics and the per-request trace unchanged.
- Decisions: [ADR 0007](decisions/0007-reuse-previous-request-in-parse.md) (reuse rule, `RecentRequests`, Session 4 must never edit client blocks in place); [ADR 0008](decisions/0008-wal-checkpoints-in-the-background.md) (timer + PASSIVE, why the WAL stays bounded). Both Proposed: owner to accept.
- Stubbed / provisional: Items are still rebuilt per request (they hold this request's block objects; ADR 0007 option 2). `scripts/bench_request_path.py` is an addition to the §13 layout.
- Open issues: `summary.max_overhead_ms` (~25 ms) is now Python's full garbage collections (every request over ~13 ms in 20 runs coincided with one; `gc.freeze()` after startup removed them in an experiment), made worse by the harness keeping every run's results. `identify()` (~2.3 ms, ~1 ms of it SQLAlchemy rebuilding its SELECT) is now the largest step at fixture size. At ~1 MB the request path is still ~19 ms (two `json.loads`, identify, validate). Both follow-ups were suggested as separate tasks.
- make check: pass (199 tests)
- Next step: owner reviews/accepts ADRs 0007-0008 and merges after PR #3. Then the GC and identify follow-ups if wanted, or Session 4 (which must build new blocks rather than edit the client's in place, ADR 0007).

## 2026-11-04 — Owner decision: request-path ADRs 0007 and 0008
- Done: [ADR 0007](decisions/0007-reuse-previous-request-in-parse.md) and [ADR 0008](decisions/0008-wal-checkpoints-in-the-background.md) accepted as written. PR #4 had been merged into `feat/session-3-tier1-evals` after PR #3 was merged into `main`, so its commits never reached `main`; this branch merges `main` back in and carries them there.
- Decisions: Session 4's `plan/apply.py` must build new blocks and never edit the client's blocks in place (ADR 0007 requirement).
- make check: pass
- Next step: merge this PR into `main`. Then the follow-ups (full-GC pauses on the request path; identify()'s fixed cost) or Session 4.
