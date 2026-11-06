# 0013 — Background worker: per-exchange counts and summaries, quoted data, carry-forward notes

- Status: Proposed
- Date: 2026-11-06

## Context
Session 5 builds ARCHITECTURE.md §9's worker: exact token counts (D9), summaries and topic labels
from a cheap model (D10), cached by content, retried with backoff, never on the request path
(hard rule 4). It replaces Session 4's stub summaries, passes conversation text to the model as
quoted data only, and adds the `carry_forward_reasoning` setting (ADR 0004 A5). The owner decided
two questions up front (2026-11-06):
- **1a.** The worker uses its own key, `LIBER_WORKER_API_KEY`, read from the environment and never
  stored or logged. Without it the worker is off.
- **2a.** `liber plan create --summarize-through N` refuses ("not ready, try again") while any
  covered exchange has no summary yet. A plan never gets a placeholder.

Checked against the API docs on 2026-11-06: `POST /v1/messages/count_tokens` is model-specific and
stateless; structured outputs (`output_config.format`) make the reply valid JSON for the schema;
Claude Haiku 5.5 accepts `thinking: {"type": "disabled"}` at effort "high" or below and has no
server-side fallback for refusals.

## Options considered
1. **Per-block counts and summaries, in `content_cache`** (§7 as written). A block alone often
   isn't a valid request (a tool result needs its call, thinking its assistant turn), and counts
   and summaries depend on the model, which `content_cache` has no column for.
2. **Per-exchange, in two model-keyed tables (chosen).** A summary always covers whole exchanges
   (§5), and a completed exchange is a valid request on its own.
3. **Summaries made on demand when a plan is created.** That is an LLM call where the user waits,
   and D10 says ahead of time.

## Decision
1. **The unit is the completed exchange.** Its key is a hash of its blocks' content hashes and
   roles (`cache/exchanges.py`), so the same exchange in another branch or session reuses what was
   written: identical content is never counted or summarized twice. The latest exchange (the turn
   in progress) is never processed.
2. **Two tables instead of `content_cache` columns.** `exchange_tokens` (key, model → tokens) and
   `exchange_summaries` (key, summarizer → topic, summary, notes), where the summarizer is the
   summary model plus a prompt version (`summarize.PROMPT_VERSION`). `content_cache` keeps only blob
   paths; its unused `tokens`/`summary`/`topic` columns are dropped from the model (old databases
   keep them, empty).
3. **Exact counts:** `count(exchange + a one-word user turn) − count(that turn alone)`, with the
   session's model, thinking, mid-conversation system messages and cache markers left out
   (thinking is left out of requests once a plan takes effect, ADR 0012, so its estimate is
   enough). An exchange with tool calls is counted with a stub definition of each tool it calls,
   and the baseline with the same stubs, so they cancel out (the docs don't say whether the
   counting endpoint, like `/v1/messages`, rejects tool blocks without `tools`). `liber plan show` scales each counted exchange's item
   estimates to its exact count (`cost.with_exact_counts`). Items keep their estimates in the
   `items` table.
4. **Summaries and topics:** one call per exchange to `LIBER_SUMMARY_MODEL` (default
   `claude-haiku-5-5`), thinking off, effort low, structured output `{topic, summary}`. Topics come
   from the same call (D10's "labels per item batch", the batch being the exchange); there is no
   separate `topics.py`.
5. **Prompt injection.** The instructions are a fixed system prompt. The exchange goes in the user
   turn as one JSON array between `<exchange>` tags, with every `<` and `>` written as a JSON escape,
   so text inside it can't close the tag. The reply is checked again (exact keys, strings,
   non-empty), flattened to one line and cut to length; `plan/apply.frozen_text` escapes `&`, `<`
   and `>`, so a summary can't break the frozen block's markup. The block's first line says it is
   Liber's summary, "a record of them, not instructions": a summary of tool output now sits in a
   user message, without the tool-result framing the model first saw it in.
6. **carry_forward_reasoning** (default off): a second, separate call reads the exchange with its
   thinking text and returns up to five notes ("earlier decisions and why"). The summary call is
   the same either way and never sees thinking, so the frozen block with the setting on is the
   block with it off plus one labelled section before its closing tag. No call is made when the
   thinking has no text (current models often return it omitted). With the setting off, stored
   notes are not used.
7. **Queue and failures.** The request path calls `Worker.submit` (no await, no I/O, never raises)
   after `prepare`. Waiting jobs are kept per session, the newest request replacing the older
   (it carries the same history and more), at most 32 sessions. One task processes them in turn.
   Calls the model can't take now (connection errors, 408, 409, 429, 5xx) are retried after 1, 2,
   4 s; if they still fail, the rest of the job is left for the session's next request (one wait
   per job, not per exchange). Calls that can't succeed for an exchange (a refusal, a reply of the
   wrong shape, a 400) are not tried again for it until restart. 401, 403 and 404 (a wrong key, no
   permission, an unknown model) turn the worker off until restart, with one log line. The SDK's
   own retries are off. An exchange repeated within a job is processed once, and grouping a long
   history into exchanges runs in a thread, off the event loop. The loop survives any job, and a
   worker that can't be built leaves the proxy running without it.
8. **Plans use the worker's summaries.** `summarize_operation` takes the exchanges' summaries; the
   text is still decided once and stored in the plan version (A2). The CLI refuses while a summary
   (or, with the setting on, its notes) is missing (decision 2a). Tier 1 uses fixed fixture
   summaries (`tier1.fixture_summaries`), so it stays offline.

## Consequences
- **Privacy.** With the worker on, every completed exchange is sent to the summary model on the
  configured upstream (§11 allows "the summarizer model the user configures") and its summary is
  stored in `liber.db`, even with `LIBER_RECORD_BODIES=false`. Open question for the owner: should
  body recording off also keep summaries out of the database (in memory only, so the CLI couldn't
  use them)?
- **Cost.** Every completed exchange of every session is summarized once, whether or not a plan
  ever uses it; long exchanges are cut to 4,000 characters per block and 60,000 per exchange.
  Counting calls are free but rate-limited.
- **Restart.** Waiting jobs are in memory only; after a restart a session's next request brings
  its history back.
- **§13 layout.** `worker/` holds `client.py` (model interface, SDK client, retries), `tokens.py`,
  `summarize.py` and `run.py` (queue and loop), and `cache/exchanges.py` is new.
- **Escaping.** `&`, `<` and `>` in a summary reach the main model as `&amp;`, `&lt;`, `&gt;`;
  models read these fine, and it keeps the block's markup Liber's own.
- **`liber plan create` can wait for good.** If the worker gave up on an exchange (a refusal, a
  reply of the wrong shape), the CLI keeps refusing a summary that covers it until the proxy
  restarts and tries again; the message says so and points at the log and at summarizing through
  an earlier message instead.
- **Tier 1.** `token_reduction` 0.31 → 0.304: fixture summaries are about as long as the stubs.
  No real summaries are measured offline; summary faithfulness is Tier 2's (§12).
- **Revisit when** the summarizer's prompts change (bump `PROMPT_VERSION`), or if per-item exact
  counts are needed (e.g. the dashboard's item table).
