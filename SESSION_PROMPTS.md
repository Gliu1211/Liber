# Liber — Cloud session prompts

**Setup (owner, once):** create the GitHub repo and commit everything in this folder (ARCHITECTURE.md, CLAUDE.md, `.claude/`, Makefile, `scripts/`, `docs/`, `.github/`, `.gitignore`). Connect the repo in claude.ai/code.

**Rules of the road**
- One prompt per session; one session = one PR. Don't add "and also…".
- Every session follows the workflow in CLAUDE.md (orient → plan → tests first → loop on `make check` → reviewer subagent → PROGRESS.md → PR). The hooks enforce the loop.
- Review and merge each PR before starting the next session that depends on it.
- **Order:** 1 → 2 → 3 → 4, then 5 and 6 in parallel, then 7.
- **Owner steps:** after Session 3 merges, run `make eval-baseline` locally, review the numbers, and commit `evals/baseline.json` in its own PR. Agents are blocked from doing this.

---

## Session 1 — Scaffold + transparent passthrough proxy

```
Follow CLAUDE.md. Task: project scaffold and a transparent passthrough proxy (ARCHITECTURE.md §1, §3 steps 1/6/7, §8, §11, §13, D4, D12).

Build:
- pyproject.toml managed by uv (Python 3.11 pinned; deps: fastapi, uvicorn, httpx, pydantic, pydantic-settings, sqlalchemy, structlog; dev: pytest, pytest-asyncio, hypothesis, ruff, mypy, respx or an equivalent fake upstream) and a committed uv.lock (create it with `uv add` / `uv lock`, never by hand). Configure ruff and mypy in pyproject so `make check` passes.
- Package layout per §13 with module docstrings.
- FastAPI app bound to 127.0.0.1; config via pydantic-settings (port, upstream base URL).
- POST /v1/messages forwarded upstream with httpx streaming; SSE events passed back as they arrive; non-streaming supported; every other path passed through unchanged.
- Header passthrough per D12; strip hop-by-hop headers; credentials never logged or stored.
- proxy/sse.py reassembles the streamed reply and extracts usage (input, output, cache read, cache write).
- Store each request/response (credentials removed), usage and latency in SQLite.
- README: setup, `make` targets, pointing Claude Code at the proxy with ANTHROPIC_BASE_URL.

Acceptance criteria (write these tests first):
- Streaming passthrough is byte-faithful against a fake upstream.
- Non-streaming requests work; upstream errors keep their status code and body.
- Unknown paths are passed through.
- No credential value appears in the database or logs.
- Usage numbers are extracted from both streaming and non-streaming responses.
- `make check` passes.
```

---

## Session 2 — Context model, validator, session chain

```
Follow CLAUDE.md. Task: context model, validator and session identification (ARCHITECTURE.md §3, §4, §5, D7). Work in liber/model/ and liber/session/, plus the minimal wiring in the request path.

Build:
- Item / Operation / Plan models (§4).
- anthropic.parse(request) -> items and anthropic.render(items, plan) -> request.
- validate.py implementing every rule in §5, returning readable violations.
- session/chain.py: canonicalization (stable JSON, cache_control stripped), the hash chain, longest-prefix lookup, branching.
- Request path: identify → register new items → render with the applied plan (empty for now) → validate → fall back to the original request on any failure; record the outcome per request.

Acceptance criteria (tests first; use Hypothesis for the property tests):
- Round trip: render(parse(r), empty plan) == r for every fixture and for generated requests.
- Validator catches: broken alternation, orphan tool_use and orphan tool_result, empty content, edited thinking blocks.
- Two conversations sharing an opening are distinguished; a divergence creates a branch; a client rewriting its own history starts a new branch.
- Any exception in parse/render/validate results in the original request being forwarded.
- Request-path overhead p95 < 10 ms on a 100-message fixture (measured in a test).
- `make check` passes.
```

---

## Session 3 — Tier 1 eval harness and gate

```
Follow CLAUDE.md. Task: the Tier 1 offline eval harness (ARCHITECTURE.md §12 Tier 1). Work in liber/evals/ and tests/fixtures/.

Build:
- liber/evals/tier1.py runnable as `python -m liber.evals.tier1 --out evals/results/latest.json`, writing the JSON format documented in scripts/eval_gate.py.
- Fixture sessions: at least 5 recorded/synthetic multi-turn sessions (scrubbed), including tool loops, thinking blocks, and a long session (100+ messages).
- Metrics: validity_rate, round_trip_identity, fallback_rate, token_reduction (0 for now, plans arrive in Session 4), prefix_stability (1.0 placeholder until Session 4), p95_overhead_ms. Each with direction and a sensible tolerance.
- Per-request trace records (plan version, validator result, before/after token estimates) written alongside results for diagnosis.
- Do NOT create evals/baseline.json; the owner creates it after review (the guard hook blocks it).

Acceptance criteria:
- `make eval` produces evals/results/latest.json deterministically (same output on repeated runs except timings).
- `scripts/eval_gate.py` fails when a metric is deliberately regressed in a test.
- `make check` passes.
```

---

## Session 4 — Edit plans, frozen blocks, break points

```
Follow CLAUDE.md. Task: edit plans and compaction (ARCHITECTURE.md D5, D6, D8, §4, §5, §6, §7). Work in liber/plan/ and liber/cache/. Write a plan first; this touches several modules.

Build:
- Versioned plans: pending vs applied, immutable versions, undo.
- Operations: offload (pointer note, blob stored by hash, tool_result pairing preserved), delete (expanding to whole pairs/exchanges), summarize range -> frozen block (stub summaries for now).
- Frozen-block rendering per D8, append-only; compaction boundaries per §5.
- Break-point policy per §6; minor vs major classification; cost.py using the pricing table from config.
- Content-addressed cache and blob store.
- CLI: `liber plan create|apply|undo|show`.
- Extend the Tier 1 harness: standard plans per fixture so token_reduction and prefix_stability are real numbers.

Acceptance criteria:
- Appending a frozen block leaves the system prompt and earlier frozen blocks byte-identical (prefix_stability = 1.0).
- Undo restores the previous render exactly.
- Edits before the frozen/live boundary are classified major; others minor.
- Every rendered request in every fixture passes the validator.
- `make check` passes (the eval gate will show the new metrics; if they legitimately move the baseline, say so in the PR so the owner can update it).
```

---

## Session 5 — Background worker

```
Follow CLAUDE.md. Task: background worker (ARCHITECTURE.md §9, D9, D10). Work in liber/worker/.

Build an asyncio worker started from the app lifespan: exact token counts via Anthropic's token-counting endpoint (verify against current docs), summaries and topic labels via a configurable cheap model, all cached by content hash, retries with backoff, never on the request path. Replace Session 4's stub summaries. Conversation text is passed to the model as quoted data only.

Acceptance criteria:
- Fake model client in tests; no network.
- Identical content is never summarized twice (cache hit test).
- A worker failure never affects the request path (test).
- Prompt-injection fixture (conversation text instructing the summarizer) does not change the summarizer's instructions or output format.
- `make check` passes.
```

---

## Session 6 — Dashboard

```
Follow CLAUDE.md. Task: dashboard (ARCHITECTURE.md §10, D11). Work in liber/dashboard/.

Build a FastAPI + Jinja + HTMX dashboard served by the same app, bound to 127.0.0.1: session list with branches; item table (role, kind, tokens, topic, status); topic map with token totals; select items -> offload / summarize / delete into the pending plan; cost/payback preview; Apply and Undo; per-session chart of input tokens and cache reads vs writes.

Acceptance criteria:
- Route tests for every action using the test client.
- Apply goes through the same plan code as the CLI (no duplicate logic).
- `make check` passes.
```

---

## Session 7 — Tier 2 quality evals

```
Follow CLAUDE.md. Task: Tier 2 quality evals (ARCHITECTURE.md §12 Tier 2). Work in liber/evals/.

Build `python -m liber.evals.tier2` (manual, uses real models, reads the API key from the environment, never runs in CI): task-success replay under full context / simulated client auto-compaction / Liber; needle-fact faithfulness with an LLM judge; real cache read/write costs from usage. Write results to evals/history/<date>.json and a markdown report comparing runs over time.

Acceptance criteria:
- Runs end-to-end against a fake client in tests.
- Refuses to run without an explicit --live flag.
- `make check` passes.
```
