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
