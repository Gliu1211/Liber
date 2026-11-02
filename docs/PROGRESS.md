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
