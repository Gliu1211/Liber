# Liber — Cloud session prompts

**Setup:** create the GitHub repo, commit `ARCHITECTURE.md` and `CLAUDE.md` at the root, connect it in claude.ai/code. One prompt per session. Review and merge each PR before starting a session that depends on it.

**Order:** 1 → 2 → 3, then 4 and 5 can run in parallel, then 6. Session 2 builds the heart of the system; review it line by line (or rewrite parts yourself) before moving on.

---

## Session 1 — Transparent passthrough proxy

```
Read CLAUDE.md and ARCHITECTURE.md.

Build the project scaffold (layout per §13, pyproject with deps, ruff, pytest, GitHub Actions CI, README with setup) and a transparent proxy:
- FastAPI app bound to 127.0.0.1, config via pydantic-settings (port, upstream base URL).
- POST /v1/messages forwarded upstream with httpx async streaming; every SSE event passed back to the client as it arrives. Non-streaming requests also supported.
- All other paths passed through unchanged.
- Header passthrough per D12; strip hop-by-hop headers; never log credentials.
- proxy/sse.py reassembles the streamed assistant reply and extracts usage (input, output, cache read, cache write tokens).
- Store each request/response (body minus credentials, usage, latency) in SQLite.
- Tests with a fake upstream (no network): streaming passthrough is byte-faithful, non-stream works, errors from upstream are passed through with the same status, credentials never appear in the DB or logs.
- README: how to point Claude Code at the proxy with ANTHROPIC_BASE_URL.

No context editing yet. The proxy must be invisible to the client.
```

---

## Session 2 — Context model, validator, session chain

```
Read CLAUDE.md and ARCHITECTURE.md (§3, §4, §5, D7). Work in liber/model/ and liber/session/.

Build:
- Item / Operation / Plan models (§4).
- anthropic.parse(request) -> items and anthropic.render(items, plan) -> request. With an empty plan, render must reproduce the original request exactly (property test this on many fixtures).
- validate.py implementing every rule in §5, returning a list of readable violations.
- session/chain.py: canonicalization (stable JSON, cache_control stripped) and the hash chain; lookup of the longest matching stored prefix; branching when histories diverge.
- Wire into the request path: identify session, register new items, render with the applied plan (empty for now), validate, fall back to the original request on any failure.
- Tests: tool_use/tool_result pairing, thinking blocks untouched, two conversations sharing an opening, a branch, a client that rewrites its own history (new branch), fallback on validation failure.
```

---

## Session 3 — Edit plans, frozen blocks, break points

```
Read CLAUDE.md and ARCHITECTURE.md (D5, D6, D8, §4, §6). Work in liber/plan/ and liber/cache/.

Build:
- Versioned edit plans: pending vs applied, immutable versions, undo.
- Operations: offload (pointer note, blob stored by hash, tool_result pairing preserved), delete (expanding to whole pairs/exchanges per §4), summarize range -> frozen block (use a stub summary for now; the worker comes later).
- Frozen-block rendering per D8, append-only; compaction-boundary rules per §5.
- Break-point policy per §6 (user apply, threshold notify, idle > cache TTL), minor vs major compaction.
- cost.py: reprocessed tokens, one-time cost, per-turn savings, payback turns, using a pricing table from config.
- Content-addressed cache + blob store (§7, §8).
- A small CLI (liber plan …) to create/apply/undo plans so this is testable before the dashboard exists.
- Tests: appending a frozen block leaves the system prompt and earlier frozen blocks byte-identical; undo restores the previous render exactly; early edits are classified as major; every rendered request passes the validator.
```

---

## Session 4 — Background worker

```
Read CLAUDE.md and ARCHITECTURE.md (§9, D9, D10). Work in liber/worker/.

Build an asyncio background worker started from the app lifespan: exact token counts via the token-counting endpoint (verify it against the current docs), summaries and topic labels via a configurable cheap model, all cached by content hash, retries with backoff, never touching the request path. Replace the stub summaries from Session 3. Untrusted conversation text is passed to the model as quoted data only. Tests use a fake model client.
```

---

## Session 5 — Dashboard

```
Read CLAUDE.md and ARCHITECTURE.md (§10, D11). Work in liber/dashboard/.

Build a FastAPI + Jinja + HTMX dashboard served by the same app: session list with branches; item table (role, kind, tokens, topic, status); topic map grouped by topic with token totals; select items -> offload / summarize / delete into the pending plan; cost/payback preview; Apply and Undo; a per-session chart of input tokens and cache reads vs writes. Keep it simple and readable.
```

---

## Session 6 — Evals harness

```
Read CLAUDE.md and ARCHITECTURE.md (§12). Work in liber/evals/.

Build a harness that replays recorded multi-turn sessions with checkable tasks under three conditions (full context, simulated client auto-compaction, Liber plan), collecting task success, input tokens, cache read/write tokens, estimated cost, and latency, and writes a markdown + CSV report. Include 3 small synthetic sessions as fixtures and document how to add real ones.
```
