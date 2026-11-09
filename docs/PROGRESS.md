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
- make check: pass (429 tests; Tier 1 unchanged, eval gate passed)
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
- make check: pass (429 tests; Tier 1 unchanged, eval gate passed)
- Next step: merge this PR into `main`. Then the follow-ups (full-GC pauses on the request path; identify()'s fixed cost) or Session 4.

## 2026-11-04 — Session 4a: edit plans, frozen blocks, blob store, CLI (Session 4 split: 4a/4b)
- Done: `plan/apply.py` (summarize → frozen blocks at the front of the first live user message, D8, append-only D5; offload → pointer notes, tool_result keeps its `tool_use_id`; delete → tool pairs and emptied exchanges; never the current turn, thinking or other-kind blocks; `PlanNotApplicable` when a plan doesn't fit); `plan/versions.py` + tables `plan_versions`, `plans` (immutable versions, pending vs applied, apply request, undo to the parent); `plan/policy.py` (a requested version takes effect at a clean turn, ADR 0004 A1); request path renders with the session's plan (candidates: pending, applied, empty; each must fit, not fail, and validate), records `plan_version` and `edit_skipped`; `cache/blobs.py` + `cache/content.py` (offloaded originals by hash, written in background tasks); `liber plan show|create|apply|undo`; Tier 1 standard plans (real `token_reduction` 0.0075, `prefix_stability` 1.0 over 7 minor compactions). Independent review run: all must-fix (orphaned server-tool pair, undo to the empty plan never completing, 5 s promotion stall, one bad plan breaking a session) and should-fix items fixed, with tests.
- Decisions: [ADR 0009](decisions/0009-edit-plans-session-4a.md) (versions, rendering formats, **edits held back while the rendered request has thinking blocks** until 4b's `drop_block`; "applied" means "sent"); [ADR 0010](decisions/0010-tier1-standard-plans.md) (standard plans, prefix_stability definition). Both Proposed: owner to accept.
- Stubbed / provisional: stub summary text until the worker (Session 5); no cache breakpoint added at the frozen boundary (open question in ADR 0009); no `frozen_blocks` table (blocks live in plan versions); `plan/versions.py` and `plan/commands.py` are additions to §13.
- Open issues: in practice 4a sends few edits (Claude Code requests nearly always contain thinking) until 4b. Stub summaries make tiny chats longer. The request-path latency test is at the edge on slow machines for main too (in-suite p95 8.6–10.9 ms on this container).
- make check: pass (Tier 1 values move the baseline: `token_reduction`, `prefix_stability`; the owner records `evals/baseline.json`)
- Next step: owner reviews 4a and accepts ADRs 0009/0010. Then Session 4b: the preserved-thinking opt-in (beta header + `drop_block`, A3 edge cases) replacing the hold-back, `input_transformations` recording (A4), idle/threshold break points, major/minor classification, `plan/cost.py` (which should refuse compactions that don't pay).

## 2026-11-04 — Owner decisions: latency budget, ADR process
- Done: ADRs 0009 and 0010 accepted and PR #6 (Session 4a) merged; ADR 0009's cache-breakpoint question resolved ("don't add one"). Request-path budget raised to 20 ms p95 (ADR 0011), defined once in `tier1.OVERHEAD_BUDGET_MS` and used by both the in-suite test and the eval gate. CLAUDE.md: new ADRs are always Proposed; no queued follow-up tasks.
- Why: on a 2-vCPU container, 4a's p95 was 7.4-9.5 ms against the old 10 ms budget, and the latency test failed 1 in 3 `make check` runs.
- Next step: owner records `evals/baseline.json` (`make eval-baseline`) in its own PR, then Session 4b.

## 2026-11-04 — Session 4b: preserved thinking on edited requests, break points, cost
- Done: stale thinking left out of edited requests (thinking from before the session's plan version took effect, a leading run; `plans.activated_at`), also under the empty plan once a session was edited; `drop_block` opt-in + `thinking-binding-controls-2026-08-01` beta merged into the client's `anthropic-beta` on every edited request where the model allows it (ADR 0004 A3: client's own setting respected, `"error"` → original sent; `between_tools` → no field; unconfirmed model with thinking → original); 4a's hold-back removed. `input_transformations` recorded per request (JSON bodies, streaming `message_start`, a later `message_delta`). §6 break points: idle (pending version applied at the next clean turn after `LIBER_IDLE_APPLY_SECONDS`, as written), threshold (`requests.context_share`; applies only with `LIBER_THRESHOLD_AUTO_APPLY`). Major vs minor classification; `plan/cost.py` with a user-editable pricing table (`LIBER_PRICING`), shown by `liber plan show`. Tier 1: `token_reduction` 0.0075 → 0.31, `prefix_stability` 1.0 over 43 minor compactions, the beta on exactly the edited requests.
- Decisions: [ADR 0012](decisions/0012-strip-stale-thinking.md), Accepted by the owner: `drop_block` alone would drop fresh reasoning on every request after a compaction (it drops every thinking block after the first mismatch), so Liber leaves stale thinking out itself. Supersedes ADR 0009 decision 4 (the hold-back). CLAUDE.md rule 6 and ARCHITECTURE §5 name the leading-run exception (owner). Idle auto-apply implemented as §6 describes (owner).
- Stubbed / provisional: threshold only flags (proposing a plan needs summaries, Session 5); model facts (adaptive-by-default, context windows, prices) are tables checked against the docs on 2026-11-04.
- Review: independent reviewer run; all must-fix fixed with tests: the activation point is now stored with the activating request's chain hash, so other branches and resent older requests leave out all completed-turn thinking (never the loop in flight), and 4a-era sessions get their point recorded at their next clean edited turn. Should-fix: Tier 1 now checks that the request a version takes effect on sends no thinking (fails if stripping breaks), cost estimates leave out stale thinking, empty `anthropic-beta` values, edge-case tests, pricing provenance noted.
- Open issues: whenever Liber sends the client's own request in an already-edited session (a Liber failure, the client switching to `"error"`, an unconfirmed model, a message only stale thinking would empty), it can carry thinking produced against Liber's rendering, which enforced accounts reject (ADR 0012 Consequences). Stub summaries still make tiny chats longer (the cost estimate shows "never pays back"). ADR 0004 (A3's strip path, its consequences) and ADR 0009 (decision 4) need the owner's "amended / superseded by 0012" notes.
- make check: pass (269 tests; Tier 1 values move the baseline: `token_reduction`)
- Proposed follow-ups (owner decides): a dashboard or CLI view of `input_transformations` per session; summaries from the worker (Session 5) so compactions stop costing tokens on small exchanges.
- Next step: owner reviews 4b, then Sessions 5 and 6.

## 2026-11-05 — Pricing tiers; fallback opt-in in edited sessions (owner-approved rule 1 amendment)
- Done: `claude-sonnet-5-5` price entry (cache read 0.10). `ModelPrice` gets an optional `long_prompt` tier (`above_tokens` + rates); Haiku 5.5 is 0.10/0.125/0.01 up to 100k tokens, 0.50/0.625/0.05 above; `cost.estimate` prices each request at the tier its estimated message tokens fall in. A fallback in a session Liber has already edited keeps the client's messages unchanged but adds the drop_block opt-in (beta + `thinking.block_binding`) when the model is confirmed, the client hasn't set `prefix_mismatch_behavior`, and the API accepts the field for its thinking setting (`pipeline.add_fallback_opt_in`, `thinking.fallback_opt_in`). CLAUDE.md rules 1 and 6 and ADR 0012's remaining-risk section updated.
- Decisions: hard rule 1 amendment (owner). The opt-in is skipped where the API would reject the field (`between_tools`, older non-adaptive settings): adding it there would itself cause a 400.
- Open issues: a failure before the conversation is identified (undecodable body, database too busy) can't know the session was edited, so it stays byte-for-byte. Tier thresholds compare message tokens only (system and tools aren't estimated).
- make check: pass (278 tests). Reviewer skipped at the owner's request.
- Next step: owner review; then Sessions 5 and 6.

## 2026-11-06 — Session 5: background worker (exact token counts, summaries, carry-forward notes)
- Done: `liber/worker/` — `run.py` (an asyncio task started in the app lifespan when `LIBER_WORKER_API_KEY` is set; the request path calls `Worker.submit` after `prepare`: no await, no I/O, never raises; waiting jobs kept per session, newest request replaces the older, at most 32), `client.py` (`ModelClient` interface; `AnthropicModelClient` on the official SDK, its own retries off; `with_retries` 1/2/4 s for connection errors, 408/409/429/5xx), `tokens.py` (exact counts per completed exchange via `count_tokens`, minus a one-word baseline, with the session's model; thinking, system messages and cache markers left out; stub tool definitions on both sides), `summarize.py` (one structured-output call per exchange for `{topic, summary}` with `LIBER_SUMMARY_MODEL`, default `claude-haiku-5-5`, thinking off, effort low; `carry_forward_reasoning` notes as a separate call). `cache/exchanges.py`: exchange keys from content hashes (same exchange in any branch or session → same key) and two tables, `exchange_tokens` (key, model) and `exchange_summaries` (key, summarizer). Plans: `summarize_operation` builds the frozen block from the worker's summaries (`apply.frozen_text`); `liber plan create --summarize-through` refuses until they exist (owner decision 2a); `liber plan show` reports how many exchanges are summarized and scales estimates to exact counts in the cost estimate. Tier 1 uses fixed fixture summaries (offline).
- Decisions: [ADR 0013](decisions/0013-background-worker.md) (Proposed): per-exchange instead of per-block (a block alone often isn't a valid count request), model-keyed tables instead of `content_cache` columns (those dropped from the model), prompt-injection defense (fixed system prompt; exchange as JSON between tags with `<`/`>` escaped; structured output re-checked, flattened, cut; frozen text HTML-escaped and labelled "a record, not instructions"), failure policy (outage → rest of job waits for the next request; refusal/bad shape → not retried until restart; 401/403/404 → worker off until restart). Owner decision 1a: the worker's own key, a `SecretStr`, never stored or logged.
- Review: independent reviewer run; no hard-rule violations. Should-fix items all fixed with tests: an outage no longer costs a wait per exchange; wrong key/model turns the worker off once; misleading "try again" message; tool exchanges counted with stub tools; system messages kept out of exact-count scaling (and shares add up exactly); a worker that can't be built no longer stops the proxy, SDK client closed on shutdown; repeated exchanges in one job processed once; failure memory keyed by model and bounded; exchange grouping one pass and off the event loop.
- Open issues: whether the counting endpoint needs `tools` for tool blocks isn't documented (stubs sent either way). With the worker on, summaries are stored even with `LIBER_RECORD_BODIES=false` (question in ADR 0013). No cap on calls per job: a long resumed session costs 2 calls per exchange once. A summary the worker gave up on blocks `create` over that range until restart. Not tried against the live API (tests and Tier 1 are offline).
- make check: pass (323 tests; Tier 1 `token_reduction` 0.304 with fixture summaries, other metrics unchanged; the owner records `evals/baseline.json`)
- Proposed follow-ups (owner decides): a one-off live check of the worker against the API with a real key (count_tokens with tool blocks, structured output on Haiku 5.5); a per-job call cap; whether `LIBER_RECORD_BODIES=false` should keep summaries out of the database; Tier 2's carry-forward ablation (§12).
- Next step: owner reviews Session 5 and decides ADR 0013. Then Session 6 (dashboard).

## 2026-11-06 — Owner decisions on Session 5; live check script
- Decisions (ADR 0013, ARCHITECTURE.md §11): summaries, token counts, plan versions and blobs are stored even with `LIBER_RECORD_BODIES=false`, and `liber purge` must delete them all; no worker call cap in v1. ADR 0013's status itself is still Proposed.
- Done: `liber/evals/live.py`, an owner-run live end-to-end check (`ANTHROPIC_API_KEY=... uv run python -m liber.evals.live`). It runs a six-turn thinking + tool conversation through a throwaway proxy, summarizes turns 1-4, applies the plan, sends two more turns, and checks: no 400s, no fallbacks, compaction sent, the API dropped no thinking (ADR 0012), context got smaller, worker wrote summaries, the planted fact survived. Decision logic tested offline (tests/test_live_check.py).
- Open: the live check hasn't been run yet (no API key). Run it before Session 6.

## 2026-11-06 — First live run; refuse plans that don't shrink requests
- Live check (owner's machine, Sonnet 5.5): 6 of 7 passed: no 400s, no fallbacks, compaction sent, the API dropped no thinking (ADR 0012), worker summaries written, the planted fact survived. "Context got smaller" failed: the check compared different requests, and the tiny test conversation's summary was bigger than what it replaced.
- Owner decision: `liber plan create` refuses a version whose requests wouldn't be smaller (ARCHITECTURE.md §6). Tests use realistic message sizes.
- Live check now compares the same request with and without Liber (the API's token count of the client's request vs what Liber sent, cached tokens included) and summarizes a ~2,000-token tool result.
- Next step: owner re-runs the live check, then Session 6.

## 2026-11-07 — Owner decision: ADR 0013 accepted
- Done: [ADR 0013](decisions/0013-background-worker.md) (background worker: per-exchange counts and summaries, quoted data, carry-forward notes) accepted, with the owner's 2026-11-06 decisions already recorded in it (summaries stored regardless of `LIBER_RECORD_BODIES`; no call cap in v1).
- Next step: owner re-runs the live check (`liber/evals/live.py`), then Session 6 (dashboard).

## 2026-11-07 — Session 6: dashboard
- Done: `liber/dashboard/` (FastAPI + Jinja + HTMX, D11), served by the proxy's own app at `/liber/`:
  - session list with branches;
  - per session: plan panel (applied/pending version, version history, the §6 cost/payback preview, Apply/Undo), summarize "through message N" (only the §5 ends `create` accepts), topic map (one row per exchange with exact token totals where the worker counted, plus the turn in progress), item table (role, kind, tokens with `~` for estimates, topic, status and pending change; tick to offload/delete), and an SVG chart of uncached input, cache reads and cache writes per request.
  - Every action calls `plan/commands.create|apply|undo`; `show` and the dashboard share `commands.SessionView`, `describe_estimate`, `describe_operation`, `live_start` and `exchange_ends`.
  - Refusals (not ready, doesn't shrink requests, bad input) show in the page.
  - HTMX 2.0.4 ships in the repo (`dashboard/static/`, 0BSD, npm tarball integrity-checked). Jinja2 is a new dependency.
  - `liber serve` logs the dashboard URL.
  - Driven in Chromium (offload, summarize, apply): works, no console errors.
- Decisions: [ADR 0014](decisions/0014-dashboard.md) (Proposed):
  - one app, `/liber/` prefix with its own 404 catch-all, so nothing under it is forwarded;
  - loopback Host only (DNS rebinding), `HX-Request` plus same-`Origin` on actions (cross-site requests), no framing plus CSP `default-src 'self'` (clickjacking);
  - HTMX served locally, so the page loads nothing from the internet;
  - topics shown per exchange (ADR 0013).
- Also: `liber plan create` (CLI and dashboard) now refuses to offload or delete thinking blocks, which the request path would refuse anyway (§5).
- Review: independent reviewer run; no hard-rule violations. All findings fixed with tests:
  - must-fix: framing (clickjacking); `/liber/` paths that missed a route were forwarded upstream;
  - should-fix: Unicode digits gave a 500; thinking only blocked in the UI; guard tests now cover all three actions and Host/Origin variants; 404 on actions for unknown sessions; a one-request chart drew nothing; the exact-count marker was wrong for system messages; summarize options now match `create`'s exchange-end rule; HTMX read lazily so a missing file can't break the proxy.
- Open issues: action messages are the CLI's wording ("To use it: liber plan apply…"). The session page loads every recorded request for the chart (fine now; large histories may want a limit). No restore of offloaded originals in the page yet.
- make check: pass (369 tests; Tier 1 unchanged, eval gate passed)
- Proposed follow-ups (owner decides): restore/view offloaded originals from the dashboard; dashboard-specific action messages; a per-request detail view (outcome, edit_skipped, input_transformations).
- Next step: owner reviews Session 6 and decides ADR 0014; re-run the live check; then Session 7 (Tier 2 quality evals).

## 2026-11-07 — Owner decision: ADR 0014 accepted
- Done: [ADR 0014](decisions/0014-dashboard.md) (dashboard) accepted as written. PR #14 (Session 6, together with the ADR 0013 acceptance) is merged.

## 2026-11-07 — Session 7: Tier 2 quality evals
- Done: `liber/evals/tier2.py` (`python -m liber.evals.tier2 --live`; refuses without `--live` or `ANTHROPIC_API_KEY`) and `liber/evals/tier2_report.py`.
  - One history per repeat (task turns with full context, thinking on, scripted tools), continued under four conditions with the task's probe and needle questions: A full context, B Liber thinking dropped, C Liber + carry_forward_reasoning, D simulated client compaction.
  - B and C run Liber's request path in-process (plans via `commands.create/apply`, like the CLI) and send its exact bytes with `prefix_mismatch_behavior: "error"`. A or D send the client's own request with the same setting.
  - B and C share the worker's summaries; only C's block has the notes.
  - An LLM judge grades the probe (rubric) and the needle.
  - Per condition: success, needle, input/output/cache tokens, thinking dropped, input-side cost, binding errors by path, other errors.
  - The report: mean and spread, the comparisons A vs B, C vs B, B/C vs D, and trends; files in `evals/history/<date>.json` and `.md`.
  - Tasks in `evals/tier2_tasks/`: `dead_end_debugging` (reasoning-dependent: the database is ruled out only in the thinking) and `migration_facts`.
- Decisions: [ADR 0015](decisions/0015-tier2-quality-evals.md) (Proposed). Shared history per repeat; Liber in-process because the proxy sends a client's `"error"` request unedited (ADR 0012); a B or C request Liber didn't compact is recorded as an error, not passed off as A.
- Tests (offline; they check plumbing, not quality):
  - The fake API enforces binding (its signature = hash of the prior history; `"error"` → 400 by path) and returns thinking text only with `display: "summarized"`.
  - End to end on both fixtures. On `dead_end_debugging`, the rigged answers come out A pass, B fail, C pass, D fail, with the needle kept in A/B/C only.
  - Breaking Liber's stale-thinking removal on purpose makes B report the failing path, counted as a fail.
  - The real HTTP client is tested on a mock transport (exact bytes, beta header, retries, non-JSON, network errors), as are the judge's failure paths, the turn errors, and the `--live`/CI/key/repeats refusals.
  - The report's comparisons, spread, verdict counts, errored runs, history files and trends are checked on a fixed dataset.
- Review: independent reviewer run; no hard-rule violations. All findings fixed:
  - must-fix: the reasoning task's evidence was also visible (now `evidence_markers`, with the report showing when B's summary kept it); thinking text omitted by default (now `display: "summarized"`, with an error when none comes back); binding breaks left out of success rates (now fails); one transient error lost the whole run (now retries, per-repeat errors, saving after each repeat); judge thinking could truncate the verdict (now thinking off, and cut-offs are `None`); my PROGRESS edit had rewritten two older entries (reverted).
  - should-fix: D's compaction call now in input tokens, averaged over complete runs only; reasoning-dependent averages split from the rest; cut-off replies and endless tool loops are errors; CI guard; tests for the HTTP client and judge; `tier2_api.py` split out; trends per task.
- Open issues:
  - Not yet run live.
  - Cost is input side only (no output prices in the table); B/C's worker calls aren't metered.
  - The reasoning task assumes the model reasons about the database in its thinking and returns thinking text.
  - `input_transformations` and the binding error format are as assumed in live.py; check on the first live run.
- make check: pass (394 tests; Tier 1 unchanged, eval gate passed)
- Proposed follow-ups (owner decides): first live run (a few dollars); paired per-repeat differences in the report; output prices in the pricing table; usage from worker calls; more tasks, including replayed real sessions.
- Next step: owner reviews Session 7, decides ADR 0015, and runs Tier 2 live. v1's roadmap (§14) is then complete.

## 2026-11-07 — Tier 2: judge fix, `--rejudge`, harder tasks
- Context: the first live run (`evals/history/2026-11-08.*`, named by its UTC date) graded nothing. All 48 judge calls returned 400: Opus 5.5 rejects `thinking: {"type": "disabled"}`. Everything else in the run was valid (0 binding errors across 48 compacted requests; B used 59–78% fewer input tokens than A). By manual reading, every condition answered both tasks correctly, so the two tasks are too easy to separate conditions.
- Done:
  - Judge (`tier2_api.py`): no `thinking` field (the model's default applies), `output_config.effort: "low"`, `max_tokens` 8,000 so any thinking can't crowd out the verdict. A failed judge call now keeps the API's own error message. The fake API rejects disabled thinking like the real one.
  - `--rejudge HISTORY_JSON` (`tier2.py`): grades the saved answers again with the current judge and task rubrics, without re-running any conversation. Writes the same `.json`/`.md`, keeps the first original as `<name>.json.bak` (never overwritten; not `*.json`, so trends ignore it), and records each re-grade, with the previous judge model, in `rejudged`. Runs now save the questions asked; an answer whose question has changed since is left as it was, with a note. Same guards as a live run (`--live`, a key, not CI), plus a clear refusal for a file that isn't a Tier 2 history.
  - During the probe and needle questions, a tool call now gets an error result ("answer from memory") instead of the scripted output, and the report lists it. Before, a condition could pass the new probes by reading the file again.
  - Three harder tasks in `evals/tier2_tasks/`:
    - `undiscussed_config_detail`: a ~90-line config read once; the probe asks for a setting nobody discussed (`retry.backoff_multiplier = 1.7`), among similar-looking numbers.
    - `derived_pool_load` (reasoning-dependent): connections in use (~31 of 50) are only ever worked out in the thinking (760 req/s × 41 ms); the visible reply is just "no".
    - `code_reconstruction`: parse() is edited twice, then the probe asks for the current function exactly.
- Tests: judge request shape (no thinking, low effort); the API's error message kept; rejudge grades saved answers without re-running, leaves runs without answers alone, skips (with a note) runs whose task file is gone or whose question changed, drops stale notes, rewrites the file in place, and a second rejudge keeps the true original; CLI guards (no API client is made before they pass; CI, directory, non-history and broken JSON refused); a tool call during the questions gets nothing back and shows in the report. New tasks are covered by the existing fixture check.
- Review: independent reviewer run; no hard-rule violations. Fixed: must-fix, probes answerable by calling the tool again (above), and ADR 0015 out of date (judge, new tasks, `--rejudge`; still Proposed). Should-fix, all done: `.bak` overwritten by a second rejudge; rejudge against a changed question; an untested missing-task path; the summary line counting runs it hadn't regraded; the previous judge model lost; `derived_pool_load` markers that couldn't match (now `41.1`, `40.8`, `db_query_latency`, `pool_size`); usage line; guard test that could reach the network. Nits done: `code_reconstruction` rubric also checks `id = parts[0]` and ignores names and quote style; `split('|')` marker; judge comment no longer claims thinking stays on; `.is_file()`; dated naming note.
- make check: pass (401 tests; Tier 1 unchanged, eval gate passed)
- Open issues: the worker still sends `thinking: disabled` to its summary model; fine on Haiku 5.5, but it would 400 on a Sonnet/Opus 5.5 worker. The new tasks' difficulty is a guess until a live run.
- Proposed follow-ups (owner decides): make the worker's thinking setting depend on its model; decide ADR 0015.
- Next step: owner merges, runs `--rejudge` on the 2026-11-08 history (judge calls only), then a fresh `--live --repeats 3` with all five tasks.

## 2026-11-07 — Tier 2 second live run: findings, two fixes, ADR 0016 proposed
- Results (`evals/history/2026-11-08-012656.*`, owner's machine; 3 repeats, Sonnet 5.5, Opus 5.5 judge, Haiku 5.5 summaries):
  - `derived_pool_load`: B 1/3, C 3/3, D 3/3, A 3/3. When B failed, its summary had the pool size and latency but not the request rate, and the model said so instead of guessing. C's notes kept the derivation. First evidence that carry-forward notes matter.
  - `undiscussed_config_detail`: A 3/3; B, C and D all 0/3, every answer "I don't know" (no guesses).
  - `code_reconstruction`: C 2/3 (one rebuilt `parse()` split the unstripped line), the rest 3/3: one answer, within noise.
  - `dead_end_debugging`, `migration_facts`: everything passed; still too easy.
  - Input tokens: Liber 44–77% below D on every task. 0 binding errors, 0 other errors, 0 tool calls during the questions. `--rejudge` worked on the first run (2026-11-08 now graded).
- Done:
  - Summarizer cuts are now marked as Liber's (`worker/summarize.py`): a cut entry carries `"cut_by_liber": true`, and both system prompts say what it means. A JSON field, not words in the text, so the conversation can't fake one (ADR 0013's rule). `BLOCK_MAX_CHARS` is 16,000 (was 4,000). Why: the 7,700-character metrics dump was cut at 4,000, the summary said "Metrics were truncated mid-output", and B then believed the tool had cut its data.
  - `PROMPT_VERSION` 2, so summaries written with the old prompt aren't reused. Side effects: on the next request in each active session the worker re-summarizes every past exchange once (a one-time worker cost), and until it catches up `liber plan create --summarize-through` says "not ready". Applied plans are unaffected (their text is fixed in the plan). No Tier 1 fixture or baseline refers to `prompt-v1`. Tool-heavy exchanges now send up to 4x more text per summary call, still bounded by the 60,000-character exchange limit.
  - `derived_pool_load` evidence markers narrowed to the request rate (`760`, `758`, `762`) and the derived answer (`31 of 50`, `31/50`, `Little's law`). "41 ms" matched "40-41 ms" in every summary, so the column said 3/3 even when the summary lacked the one figure that matters; plain "Little" would match "a little".
- Decisions: [ADR 0016](decisions/0016-keep-items-through-a-summary.md) (Proposed): keep chosen items through a summary (copied as they were into the frozen block), and Tier 2 condition E to measure it. Today a summary replaces everything in its range, so a user can't keep the config output without leaving the whole rest of the conversation unsummarized.
- Tests: a cut block carries the flag and both prompts explain it; text imitating the flag stays text; an exchange over the limit is cut evenly, flagged and fits; a tiny share still keeps text; a 7,700-character block is sent whole.
- Review: independent reviewer run; no must-fix. Fixed: the text mark left no room for content in big exchanges and could be faked (now the JSON flag); the `shortened()` path untested; `"Little"` matched "a little"; ADR 0016 said kept text is escaped like summaries (now: only Liber's tag sequences are neutralized, so code reads as it was), and now lists the prompt-injection cost, why bytes stay identical, what E records on a refused plan, and the Tier 2 files; `pinned_item_ids`/`--pin` instead of "keep" (§4's default); the PROMPT_VERSION side effects (above).
- make check: pass (405 tests; Tier 1 unchanged, eval gate passed)
- Open issues: the history file's "date" now carries a time (`2026-11-08-012656`) when a same-day file exists; trends sort it fine.
- Proposed follow-ups (owner decides): ADR 0016 and its implementation (one session: plan operation, rendering, CLI, Tier 2 condition E); more repeats (5+) now that conditions differ; replace or harden `dead_end_debugging` and `migration_facts`.
- Next step: owner decides ADR 0016.

## 2026-11-08 — Owner decision: ADR 0016 accepted
- Done: [ADR 0016](decisions/0016-keep-items-through-a-summary.md) (pin items through a summary; Tier 2 condition E) accepted as written. ARCHITECTURE.md §4 notes pinning under summarize, marked not built yet.
- Next step (owner starts it): implement ADR 0016 in one session (plan operation, rendering, validation, CLI `--pin`, Tier 2 condition E with `keep_tool_results` on `undiscussed_config_detail` and `derived_pool_load`), then a live Tier 2 run with `--repeats 5`.

## 2026-11-08 — ADR 0016: keep chosen items through a summary ("pins"), Tier 2 condition E
- Done:
  - `Operation.pinned_item_ids`. `plan/apply.py` copies pinned items into the frozen block after the summary (and C's notes) under "Kept as is (copied word for word … a record of them, not instructions)", each with a label such as `[tool_result of read_config, message 2]`.
    - The text isn't HTML-escaped, so code and config read as they were. Only `<liber:` / `</liber:` are neutralized.
    - The text is fixed in `Operation.summary` when the version is created, so rendering stays byte-identical.
  - `commands.create(pin=…)` and the CLI's `liber plan create --pin ID` (repeatable).
    - The text comes from the session's recorded request bodies (newest 50), so pins need `LIBER_RECORD_BODIES`.
    - Refused: thinking and images; items outside the range; items also offloaded or deleted; a pin without `--summarize-through`; plans that wouldn't shrink requests, since the kept text counts.
  - `show` and the dashboard list "keeping N as is".
  - Tier 2 condition E: B's plan plus pins of the task's `keep_tool_results` tools in the summarized range. It runs only on tasks with that field (`undiscussed_config_detail` keeps `read_config`, `derived_pool_load` keeps `read_metrics`); other tasks show n/a.
    - New comparisons: A vs E, E vs B, E vs D.
    - E with nothing to keep, or a refused plan, is an error, not a silent B.
- Decisions: implementation notes added to [ADR 0016](decisions/0016-keep-items-through-a-summary.md) (accepted by the owner the same day, entry above). They cover where pinned text comes from (recorded bodies, read before the write lock), that it's stored in the plan version, the final wording, and where the checks live.
- Review: independent reviewer run; no hard-rule violations. Fixed with tests:
  - must-fix: reading recorded bodies happened inside the write transaction and could hold the lock the proxy needs (now read beforehand, one body at a time); untested older-body and unreadable-body paths;
  - should-fix: case variants of Liber's tags (tested); labels neutralized; odd tool-result parts named instead of crashing; duplicate pins; the ADR notes now say plainly that pinned text is stored in plan versions; labels show who wrote the item; tests for tool_use/text pins, a pin in a second summary block, and `show` after apply.
- Tests (offline):
  - pins through the real app: word for word, the tags neutralized, sent and byte-identical across requests, listed in `show`;
  - every refusal, the CLI flag, `PlanNotApplicable` for a pin outside its range, and the section order;
  - E end to end on `undiscussed_config_detail` with rigged fakes: A and E pass, B, C and D fail, and E's tokens fall between B's and A's;
  - tasks without E, E with nothing to keep, and the report's E comparisons.
- make check: pass (429 tests; Tier 1 unchanged, eval gate passed)
- Open issues: `derived_pool_load`'s metrics dump is ~7,700 characters, so E's plan there may be refused as not smaller; a live run will tell. The dashboard checkbox for pins isn't built.
- Proposed follow-ups (owner decides): pin checkboxes in the dashboard (by item or topic); ARCHITECTURE.md §4 wording once ADR 0016 is accepted; a live Tier 2 run with E.
- Next step: owner reviews and decides ADR 0016, then runs Tier 2 live with E.

## 2026-11-08 — ARCHITECTURE.md §4: pins as built
- Done: ADR 0016 had already been accepted on `main` (PR #19), and that is now merged into this branch. ARCHITECTURE.md §4 now describes pinning as built, replacing "accepted, not built yet": the `--pin` CLI, the allowed kinds, no escaping beyond Liber's tags, no overlap with offload or delete, text from recorded bodies fixed in the plan version, and counted toward §6's "must shrink" rule. ADR 0016's implementation notes say the same.
- Next step: owner reviews PR #20 (the implementation), then a live Tier 2 run with `--repeats 5`.

## 2026-11-09 — Tier 2 run 2026-11-08-222804: eval fix, size estimate fix, a pin task, ADR 0017 proposed
- Findings (owner's run, 5 repeats):
  - pinning recovered `derived_pool_load` without notes (E 5/5 vs B 2/5);
  - E's 5/5 on `undiscussed_config_detail` was mostly not E: 4 of 5 plans were refused, and the questions then went out unedited (condition A) but were counted as E;
  - E on `derived_pool_load` sent more than full context;
  - both pin tasks were the wrong shape;
  - Liber's summaries can't hold code (`code_reconstruction`: B/C 0/5, D 5/5).
- Done:
  - Tier 2: a refused plan, E with nothing to pin, or a request Liber would send unedited now ends that condition with no verdict. No question is asked or sent, and the report shows it as `[k/n]` with the reason under Other errors. On the 2026-11-08 data, E on the config task would read 100% [1/5].
  - Plan sizes leave thinking out on both sides (`plan/cost.py`): `show`, the dashboard, cost estimates and the "must shrink" check (ARCHITECTURE.md §6 updated).
    - Why: on `derived_pool_load`, E dropped every earlier thinking block and still cost about 4% more than full context. Dropping that thinking saved ~nothing at the API, while its estimate (length including the signature) counted as a large saving. That let a plan that grew the request pass the check.
    - The pinned metrics themselves were on both sides of the comparison, so their denser tokenization (~1.8 characters per token vs the estimate's 4) cancelled out.
    - The thinking left out is still listed on its own.
  - New task `small_config_in_long_session`: a ~300-character config with an undiscussed value (`cache.ttl_seconds = 270`) among ~11,000 characters of logs plus chat in the summarized range; E keeps `read_config`. This is the shape pinning is for.
- Decisions:
  - Owner: thinking isn't counted in plan sizes (2026-11-09).
  - [ADR 0017](decisions/0017-keep-code-in-summaries.md) (Proposed, not built): keep the assistant's code word for word in summaries. The summarizer chooses candidate ids (fenced code in assistant replies, code fields of assistant tool calls) and Liber copies the bytes. Tool output is excluded (pins cover it). Capped at 4,000 characters per exchange, with a Tier 2 control (E pinning the assistant's code) first.
- Tests:
  - the refused plan asks nothing and gets no verdict (`n/a [0/1]`);
  - an unedited Liber request isn't sent (with an unconfirmed model, the probe still goes out compacted and the needle request doesn't);
  - plan sizes ignore dropped thinking;
  - the new task end to end with rigged fakes: E's plan is accepted and keeps the value for under half of A's tokens.
- Review: independent reviewer run; no must-fix. Fixed with tests:
  - the docs (cost.py, §6) now call leaving thinking out a conservative owner choice pending a real count, and name its cost: where thinking is billed, its saving isn't shown;
  - `rendered_tokens`' parameter is renamed `uncounted`;
  - the comment says the no-verdict rule covers B, C and E;
  - `--rejudge` gives no verdict to older runs whose plan was refused (the 2026-11-08 E runs), so re-reporting that file no longer shows the inflated E;
  - stronger assertions, plus tests for an unsent probe (no questions at all), the shrink check refusing a plan whose only "saving" is thinking, and `show`'s wording;
  - ADR 0017's gaps closed: a per-range budget, what happens to a block over the limit, overlap with pins, ids checked in code, bytes from decoded strings, and cache interplay.
- make check: pass (437 tests; Tier 1 unchanged, eval gate passed)
- Open issues:
  - What prior-turn thinking costs at the API is inferred from totals on one task. A `count_tokens` call on the same request with and without its thinking would settle it (needs a key).
  - Absolute size estimates still use 4 characters per token, which undercounts number-dense content.
- Proposed follow-ups (owner decides): decide ADR 0017; a live Tier 2 run including `small_config_in_long_session`; that `count_tokens` check; scaling estimates by the worker's exact counts in the shrink check.
- Next step: owner reviews this PR and decides ADR 0017.

