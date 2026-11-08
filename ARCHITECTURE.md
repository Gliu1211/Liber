# Liber — Architecture

Liber is a local API proxy that lets a user decide what stays in an LLM's context — keep, summarize, offload, or delete individual items — instead of relying on the client's blind auto-compaction.

Status legend: **Decided** = settled by the owner. **Provisional** = a reasoned default; implement it, but keep it easy to change and flag concerns in the PR.

---

## 1. System overview

```
 Client (Claude Code, any Anthropic-format app)
   │  ANTHROPIC_BASE_URL=http://127.0.0.1:<port>
   ▼
 Liber proxy (FastAPI) ──────────────► Model provider (Anthropic Messages API)
   │   ▲                                   │ streamed response (SSE)
   │   └───────────────────────────────────┘ passed straight back to client
   ├── Context store (SQLite + content blobs on disk, keyed by hash)
   ├── Background worker (token counts, summaries, topic labels)
   └── Dashboard UI (sessions, items, topic map, edit plan, apply/undo)
```

### The core idea

Model APIs are stateless: the client resends the **entire** conversation on every request. Liber never owns the history. It stores an **edit plan** per conversation and applies it as a **transform** to each request as it passes through. The client's own history is never modified, which makes undo trivial (drop or revert the plan).

---

## 2. Decision records

### D1 — Integration point: API proxy · Decided
- **Options:** (a) API proxy, (b) Claude Code hooks/transcript editing, (c) browser extension on chat apps.
- **Chosen:** (a). Sees the full context on every call; works with any client that can set a base URL.
- **Rejected:** (b) ties Liber to one tool; (c) consumer chat apps keep context server-side, out of reach.

### D2 — API format: Anthropic Messages API first · Decided
- Claude Code speaks this format, so the owner can dogfood Liber on daily work immediately.
- An OpenAI-compatible adapter comes later as a separate parse/render pair (see §4).

### D3 — Offload = pointer note + undo; no recall in v1 · Decided
- **Options:** (a) pointer + undo, (b) proxy re-inserts offloaded items via search before forwarding, (c) inject a `recall(id)` tool and run the tool loop inside the proxy.
- **Chosen:** (a) for v1. (c) is the v2 stretch goal: it requires the proxy to intercept the model's tool call (the client doesn't know the tool exists), execute it, send a follow-up request, and stitch the streams.

### D4 — Stack: Python + FastAPI · Decided
- Owner's strongest language; streaming code must be explainable line by line in interviews.
- `httpx` (async) for upstream calls, SQLite via SQLAlchemy 2.0, `pydantic` v2 models.

### D5 — Cache-aware layout: append-only frozen blocks · Decided
- Provider prompt caching is a **prefix** cache: because of causal attention, a token's cached state depends on every token before it. Appending is cheap; editing earlier content invalidates everything after the edit point.
- Therefore the rendered context is laid out as:
  ```
  [ system prompt + tools ]   untouched → always cache-hits
  [ frozen blocks ]           append-only; Liber never auto-edits a frozen block
  [ live region ]             recent turns; where new messages and minor edits land
  ```
- Edits are applied **in batches at break points**, never automatically on every request (§6).

### D6 — Minor vs major compaction · Decided
- **Minor:** frequent, cheap; appends a new frozen block at the frozen/live boundary.
- **Major:** rare, expensive; rebuilds the whole rendered context (merge small frozen blocks, re-summarize). Triggered by user edits to early content or by idle time (cache already expired).
- Analogy: minor/major GC, or LSM-tree compaction.

### D7 — Session identification: message hash chain · Provisional
- Each client message is canonicalized (stable JSON, `cache_control` markers stripped) and hashed into a chain: `h_i = H(h_{i-1} || canonical(msg_i))`, with `h_0 = H(system || tools)`.
- On each request, find the stored chain with the **longest matching prefix**. That identifies the conversation and how many messages are already known; the rest are new.
- Conversations form a tree: a divergence creates a branch, not a corruption. (Same idea as git commit hashes.)
- If the client rewrites its own history (e.g. the client's auto-compaction), the match will be short or zero → treat as a new branch/session. Recommend users turn off client-side auto-compaction while using Liber.

### D8 — Frozen-block rendering: prepend into the first live user message · Provisional
- Frozen blocks are rendered as leading text blocks of the **first user message of the live region**:
  `messages[0] = user: [frozen_1, frozen_2, …, frozen_k, <original first live user content>]`
- This keeps strict user/assistant alternation without inventing synthetic assistant messages, and appending `frozen_{k+1}` leaves the system prompt and `frozen_1…k` byte-identical (cache still hits up to there).
- Each frozen block is clearly labeled, e.g. `<liber:summary block="3" covers="msgs 12–40">…</liber:summary>`.
- Because this edits history, the thinking after the edit point is dropped once per compaction (ADR 0004). Rendering must be byte-identical across requests (A2) so later reasoning stays valid.

### D9 — Token counting · Provisional
- Ground truth for totals: the `usage` fields in every API response (`input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`).
- Per-item counts: computed by the worker via Anthropic's token-counting endpoint, cached by content hash; a fast local estimate is used until the exact count arrives. *(Verify the current endpoint against docs.claude.com.)*

### D10 — Summaries and topics via a cheap model in the background worker · Provisional
- Summarizer and topic labeler use a small, cheap model (configurable). They run **ahead of time**, so the request path never waits on an LLM.
- v1 topic map = LLM labels per item batch. Embedding clustering is a later option.

### D11 — Dashboard: FastAPI + Jinja + HTMX · Provisional
- Keeps the whole project in Python with minimal frontend tooling. A richer topic-map visualization can be added later.

### D12 — Auth: pass the client's credentials through, never store them · Decided by design
- Forward `x-api-key` / `authorization`, `anthropic-version`, `anthropic-beta` unchanged. Never persist or log credential headers.

---

## 3. Request path

For `POST /v1/messages`:

1. **Receive** the request; keep the raw JSON.
2. **Identify** the session via the hash chain (D7).
3. **Parse** into typed items (§4) and register any new items (ID, hash, estimated tokens).
4. **Render**: apply the session's *currently applied* edit plan → new request body (frozen blocks + transformed live region).
5. **Validate** the rendered body (§5). If invalid, log the error and **fall back to forwarding the original request unmodified** — Liber must never break the user's session.
6. **Forward** upstream with streaming; pass every SSE event straight back to the client as it arrives.
7. **Record** while streaming: reassemble the assistant reply into items, store the `usage` numbers (tokens, cache reads vs writes) and latency.

All other endpoints (e.g. token counting, model listing) are passed through unchanged.

**Latency budget:** steps 2–5 must add at most **20 ms at p95**, measured by the Tier 1 replay and the in-suite request-path test (ADR 0011); typical requests take a few milliseconds. The model itself takes seconds, so 20 ms is under 1% of the wait, while still catching real regressions. No LLM calls, no network calls other than the upstream forward.

---

## 4. Context model

### Items
Every content unit becomes an `Item`:

| Field | Meaning |
|---|---|
| `id` | stable ID (content hash + position in chain) |
| `session_id`, `msg_index`, `block_index` | where it lives in the client history |
| `role` | user / assistant |
| `kind` | text, tool_use, tool_result, image, document, thinking, other |
| `content_hash` | SHA-256 of canonical content |
| `tokens` | exact or estimated (flagged) |
| `topic` | label from the worker, optional |
| `depends_on` | e.g. a tool_result → its tool_use |

### Operations (per item or contiguous range)
- **keep** — default.
- **summarize** — a range of whole exchanges becomes one frozen block (D5, D8).
  - **pin** (ADR 0016; accepted, not built yet): items inside the range can be pinned. Their text is copied, as it was, into the frozen block under a labelled section, while the rest of the range is summarized. Text, `tool_use` and `tool_result` items only; never thinking.
- **offload** — content replaced by a pointer note; original stored on disk by hash. For a `tool_result`, keep the block and its `tool_use_id`, replace only its content, so pairing stays valid. Example note:
  `[Liber: offloaded item 3f2a91 — "npm install build log", 4,812 tokens. Stored locally; restore from the Liber dashboard.]`
- **delete** — remove the item. Deleting half of a tool pair deletes both; a deletion that would leave a message empty removes the message's whole exchange.

### Edit plans
- A plan is an ordered, versioned list of operations per session. Versions are immutable; undo = revert to a previous version.
- **Pending plan** (edited in the dashboard) vs **applied plan** (used by the proxy). Pending becomes applied only at a break point (§6).

### Format adapters
`parse(request) → items` and `render(items, plan) → request` are the only format-specific code. Anthropic first; an OpenAI adapter implements the same pair later. For speed, `parse` can take the conversation's previous request and reuse its hashes for unchanged messages, with an identical result (ADR 0007).

---

## 5. Validity rules (validator)

The rendered request must satisfy the Messages API rules. At minimum:
- First message is from the user; roles alternate.
- Every `tool_use` in an assistant message has its matching `tool_result` in the following user message, and vice versa.
- No empty messages or empty content.
- **Never modify thinking / redacted_thinking blocks**, and never edit the in-flight tool loop (the latest assistant turn with tool calls plus its results). The one exception: once a plan version has taken effect in a session, the thinking blocks produced before that point are stale and are left out as a whole leading run, oldest first (ADR 0012). The validator checks that the thinking sent is exactly the tail of the client's.
- **Preserved thinking (ADR 0004):** on current models a thinking block is only valid if everything before it is unchanged. Any request Liber edits must carry the `thinking-binding-controls-2026-08-01` beta and `thinking.block_binding.prefix_mismatch_behavior: "drop_block"` (merged into the client's headers and `thinking` fields), unless ADR 0004 A3 says to strip or skip instead. Otherwise forward the original.
- Preserve the client's `cache_control` markers where their target still exists; drop markers on content that was removed; never exceed the API's maximum number of breakpoints; add one breakpoint at the end of the frozen region when there is room. *(Verify the current breakpoint limit.)*

Compaction boundaries:
- A summarize range always covers **whole exchanges**: it starts at the beginning of the live region and ends just before a user message that starts a new exchange (one with no `tool_result` blocks).
- A range never splits a tool pair and never includes the in-flight tool loop.

If validation fails → forward the original request and record the failure.

---

## 6. Edit policy and break points

Edits move from pending to applied only at a **break point**, and only when the request ends at a **clean turn boundary**: its latest message is a user turn with no `tool_result` blocks (ADR 0004 A1). Otherwise the plan stays pending until the next clean boundary. The break points are:
1. **User clicks Apply** in the dashboard.
2. **Threshold:** context exceeds a configurable share of the window (default 70%). v1 behavior: notify and propose a plan in the dashboard; auto-apply is **opt-in**.
3. **Idle:** the session has been idle longer than the provider's cache lifetime (configurable, default 5 minutes) — the cache is already gone, so applying edits costs nothing extra. Pending edits are applied on the next request.

Early edits (anything before the frozen/live boundary) trigger a **major compaction** and the dashboard shows, before applying:
- tokens that will be reprocessed, estimated one-time cost, estimated per-turn savings, and the payback period in turns.
- Pricing ratios come from a user-editable config table, not hard-coded claims.

**A plan must make requests smaller** (owner, 2026-11-06). `liber plan create` estimates the request size with the applied version and with the new one (the same estimate `liber plan show` prints, ADR 0012's stale thinking included) and refuses the new version unless it is smaller. Every compaction costs a cache restart and drops older reasoning; one that saves nothing isn't worth it. Typical cases: a summary of short exchanges, or offloading an item smaller than its pointer note.

Optional v1 auto-rules (off by default, deterministic, no LLM): e.g. "offload tool results larger than N tokens that are older than K turns".

---

## 7. Caching (two layers)

1. **Provider prefix cache** — not controlled by Liber; designed around via D5/D6 and §6. Every request logs cache reads vs writes from `usage`, so cost claims are measured, not estimated.
2. **Liber's content-addressed cache** — fully controlled: `content_hash → {tokens, summary, topic, blob path}`. Hashes never change, so entries are reusable across sessions; LRU eviction for in-memory layers; summaries are never recomputed for identical content.

---

## 8. Storage

- SQLite database under `~/.liber/liber.db`; blobs under `~/.liber/blobs/<hash[:2]>/<hash>`.
- Tables (initial): `sessions`, `chain_nodes` (hash chain / branches), `items`, `plans`, `plan_versions`, `frozen_blocks`, `content_cache`, `requests` (usage, latency, validation result).
- Migrations via Alembic once the schema settles; plain `create_all` is fine for the first sessions.

---

## 9. Worker

A background asyncio task (same process in v1) that processes new items:
- exact token counts (D9), summaries for candidate ranges, topic labels (D10);
- writes results to the content-addressed cache;
- never blocks the request path; failures are retried with backoff and logged.

---

## 10. Dashboard (v1)

- Session list (with branches), item table: role, kind, tokens, topic, status.
- Topic map: items grouped by topic with token totals.
- Select items/ranges → offload / summarize / delete → **pending plan** with cost/payback preview → Apply → undo via version history.
- Per-session chart of input tokens, cache reads vs writes over time.

---

## 11. Security and privacy

- Bind to `127.0.0.1` only by default.
- Never store or log credential headers.
- Context often contains code and secrets: everything stays local; nothing is sent anywhere except the upstream provider the client already uses (and the summarizer model the user configures).
- Recording request bodies is on by default for undo/evals and can be disabled with `LIBER_RECORD_BODIES=false`. That setting covers request/response bodies only: summaries, exact token counts, plan versions and offloaded blobs are always stored, because compaction needs them (owner, 2026-11-06, ADR 0013). A `liber purge` command deletes all stored data, all of these included.
- Independently of recording, the running proxy keeps the latest decoded request of up to 8 recent conversations in memory (ADR 0007); nothing of it is written to disk.

---

## 12. Evaluation and tracing

Evaluation is **continuous**, not a final step. It runs as a loop: **trace → eval → diagnose → fix → gate.**

### Tier 1 — Offline gate (every change, every PR, deterministic, free)
Runs against recorded fixture sessions with a fake model; no network, no API keys.

| Metric | Gate |
|---|---|
| Validity rate: rendered requests passing the validator | must be 100% |
| Round-trip identity: empty plan reproduces the original request | must be 100% |
| Fallback rate: requests forwarded unmodified due to errors | must not increase |
| Token reduction on fixture sessions with standard plans | must not drop more than tolerance |
| Prefix stability: requests whose bytes up to the frozen/live boundary are unchanged after a minor compaction | must be 100% |
| Render determinism: the same history + plan version renders to identical bytes (ADR 0004 A2) | must be 100% |
| Request-path overhead (p95, steps 2–5 of §3) | must stay under budget (20 ms, ADR 0011) |

Results are written to `evals/results/latest.json` and compared to the committed `evals/baseline.json` by `scripts/eval_gate.py`. Any metric outside tolerance fails the gate.

**Baseline rule:** the baseline only changes in a dedicated PR that runs `make eval-baseline` and explains why the new numbers are correct. Never edit the baseline to make a failing change pass.

### Tier 2 — Quality evals (manual or scheduled, real models, costs money)
- **Task success replay (four-condition ablation, ADR 0004 A5):**

  | Condition | Represents |
  |---|---|
  | A. Full context, no compaction | upper bound |
  | B. Liber, thinking dropped | Liber's default |
  | C. Liber + `carry_forward_reasoning` | the carry-forward notes |
  | D. Client's own auto-compaction | the status quo |

  A vs B = cost of dropping thinking; C vs B = how much carry-forward recovers; B/C vs D = whether Liber beats the status quo. B and C use identical summaries apart from the notes. Each condition runs several times (outputs vary); report mean and spread. Fixtures include reasoning-dependent tasks (e.g. a debugging session where a dead end was ruled out only in the thinking).
- **Per condition:** task success, input and output tokens, thinking dropped (from `input_transformations`), and cost.
- **Binding checks:** Tier 2 sends `prefix_mismatch_behavior: "error"`, so an unintended binding break shows up as a 400 naming the failing block.
- **Summary faithfulness:** "needle" facts planted early in a session must stay answerable after compaction; an LLM judge scores each summary for omissions and errors.
- **Cost:** real cache read/write tokens from `usage`.
- Tier 2 is a **release gate**, not a merge gate: results are tracked over time in `evals/history/`, and a regression blocks a release until diagnosed.

### Tracing and diagnosis
- Every plan application and compaction decision is logged with before/after token counts, the plan version, and the validator result; Langfuse export is optional.
- When an eval regresses, the PR (or an issue) includes a short diagnosis: which fixture failed, which trace shows why, and what the fix changes.

Target resume line: "cut context tokens X% while retaining Y% task success across N tasks."

---

## 13. Module layout

```
liber/
  app.py              FastAPI app, routes, lifespan (starts worker)
  config.py           settings (port, upstream URL, thresholds, pricing table)
  proxy/
    forward.py        upstream httpx client, header passthrough, streaming
    sse.py            parse SSE events to reassemble the assistant reply
  model/
    items.py          Item, Operation, Plan models
    anthropic.py      parse() / render() for the Messages API
    validate.py       validity rules (§5)
  session/
    chain.py          canonicalization + hash chain (D7)
    store.py          DB access
  plan/
    apply.py          render plan onto items, frozen blocks (D8)
    versions.py       immutable plan versions, pending vs applied, undo (§4)
    policy.py         break points, minor/major compaction (§6)
    cost.py           reprocess/payback estimates
    commands.py       `liber plan show|create|apply|undo` (until the dashboard)
  cache/
    content.py        content-addressed cache
    blobs.py          on-disk blob store
  worker/
    tokens.py  summarize.py  topics.py
  dashboard/          Jinja templates + HTMX routes
  evals/              harness, metrics, replay
tests/
  fixtures/           recorded requests/responses (scrubbed of keys)
evals/
  baseline.json       committed Tier 1 baseline (protected)
  results/            generated, git-ignored
  history/            Tier 2 results over time
scripts/eval_gate.py  compares results to baseline
docs/PROGRESS.md      session handoff log
docs/decisions/       ADRs
.claude/              settings, hooks, reviewer subagent
Makefile              setup, check, fmt, eval targets
```

---

## 14. Roadmap

- **v1:** passthrough proxy → context model + validator → Tier 1 eval gate → plans + frozen blocks → worker → dashboard → Tier 2 quality evals. Every step after the gate exists must pass it.
- **v2:** `recall` tool loop (D3c), OpenAI adapter (D2), embedding-based topic map, smarter auto-rules.

## 15. Engineering practices and agent harness

Most code in this repo is written by coding agents in stateless sessions. The harness is designed so that quality comes from **deterministic checks and enforced workflow**, not from instructions an agent might forget.

### Principles
1. **One command, everywhere.** `make check` is the single definition of "done". Agents (via the Stop hook), the owner (locally), and CI all run the same command.
2. **Deterministic guardrails over prose.** Anything that must never happen is enforced by a hook, a test, or CI, not only written in `CLAUDE.md`.
3. **Progressive disclosure.** `CLAUDE.md` stays short and points here; agents read only the sections their task needs, which keeps their context focused.
4. **Small, reviewable units.** One task = one session = one PR. Conventional-commit titles (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).
5. **Tests first from acceptance criteria.** Each session prompt lists acceptance criteria; they become failing tests before implementation.
6. **Work isn't graded by its author.** An independent `reviewer` subagent with fresh context reviews every diff against this document before the PR.
7. **Explicit handoff between sessions.** Sessions share no memory, so state lives in the repo: `docs/PROGRESS.md` (log) and `docs/decisions/` (ADRs).
8. **Reproducible environment.** Dependencies are locked with `uv`; Python version pinned; CI uses the lockfile.
9. **Least privilege.** Agents can't read `.env` files, edit protected files, or run destructive git/shell commands (enforced in `.claude/settings.json` and hooks).
10. **Observability by default.** Structured logs (structlog) and per-request traces (§12) so failures can be diagnosed from evidence.

### `make check` (the quality gate)
| Step | Tool | Purpose |
|---|---|---|
| Format check | `ruff format --check` | consistent style, no diffs in review |
| Lint | `ruff check` | bugs and smells |
| Types | `mypy` on `liber/` | catches interface drift between modules |
| Tests | `pytest` (unit, property via Hypothesis, integration with fake upstream) | behavior |
| Eval gate | `scripts/eval_gate.py` | Tier 1 metrics vs baseline (§12); skipped until the harness exists |

### The inner loop
1. **Implement** the smallest next piece.
2. **Check:** `make check`.
3. **Diagnose** failures from the output before changing code; don't guess.
4. **Fix** and return to step 2.
5. **Stop** only when `make check` passes.

### Enforcement (`.claude/`)
| Hook | Event | What it enforces |
|---|---|---|
| `stop_check.sh` | Stop | Runs `make check`; blocks finishing (exit 2) and feeds the failure back while it fails. At most 3 blocks per failing streak, so it can never loop forever. |
| `guard.py` | PreToolUse (Edit/Write/Bash) | Blocks edits to protected files (`evals/baseline.json`, `.env*`, `.claude/`, `uv.lock` by hand) and destructive commands (`rm -rf` outside the repo, `git push --force`, `git reset --hard`, reading `.env`). |
| `format.sh` | PostToolUse (Edit/Write) | Auto-formats edited Python files with ruff; never blocks. |

The `reviewer` subagent (`.claude/agents/reviewer.md`) checks a diff for: violations of the hard rules, divergence from this document, missing tests for acceptance criteria, and code the owner couldn't explain.

CI (`.github/workflows/ci.yml`) runs `make check` on every push and PR, so the gate holds even if a hook is skipped.

### Handoff artifacts
- `docs/PROGRESS.md` — append-only session log: date, task, what was done, decisions, open issues, next step.
- `docs/decisions/NNNN-title.md` — ADRs for any decision not already in §2 (template in `0000-template.md`).
- PR description — "How it works" walkthrough, acceptance criteria checklist, anything stubbed or provisional.

### The owner's role
- Approves plans for multi-module tasks, reviews every PR line by line, and merges. Core modules (`model/`, `session/`, `plan/`) get the closest review; rewriting parts by hand is encouraged.

## 16. Open questions

- Summary quality checks (how to detect a summary that lost something important).
- Whether to merge small frozen blocks during minor compaction or only in major.
- How to present branches in the dashboard without confusing users.
