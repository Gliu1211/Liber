# 0004: Preserved thinking vs. Liber's history edits

- Status: **Accepted** (owner, 2026-11-02): Option 1 with amendments A1–A5 below.
- Date: 2026-11-02

## Context
ARCHITECTURE.md §5 says "never modify thinking blocks", and Session 2's validator enforces that
(`thinking_unchanged`, `in_flight_loop`). The current API docs (checked 2026-11) add a stricter
rule that ARCHITECTURE.md predates: **preserved thinking**.

On current models (Claude Fable 5.1, Opus 5.5, Sonnet 5.5, Haiku 5.5, Mythos 5.1), a thinking
block's `signature` records the conversation prefix that produced it: the top-level `system`,
the tool set, and every message before the block. When the transcript comes back, the API
checks that prefix is unchanged (the "history-editing check"):

- **Enforced (HTTP 400)** for accounts created on or after 2026-08-31 on Fable 5.1, Opus 5.5 and
  others, for every account on Mythos 5.1, and for any request that sets
  `thinking.block_binding.prefix_mismatch_behavior`. Other accounts are recorded but not
  rejected today.
- Thinking blocks can be dropped from the *front* of the history, but editing anything before a
  kept thinking block invalidates it and every thinking block after it.

Liber's whole job (summarize, offload or delete early items, D5/D6/D8) edits history before
later thinking blocks. Under the check, the first edited request after a compaction would be
rejected for enforced accounts. That breaks hard rule 1, unless Liber does something about it.

Session 2 is unaffected: it never changes a request (empty plan), and it forwards the client's
bytes unchanged.

## Options considered
1. **Opt into dropping:** on every request Liber *edits*, send the `thinking-binding-controls-2026-08-01`
   beta header and `thinking.block_binding.prefix_mismatch_behavior: "drop_block"`. The API then
   drops the first mismatched thinking block and every one after it (including in the in-flight
   tool loop) and proceeds. The response's `input_transformations` lists what was dropped.
   - Cost: the model loses its earlier reasoning after the edit point, and Liber adds a beta
     header plus a `thinking` sub-field.
   - Fit: works with D5/D8 as designed.
2. **Strip thinking blocks after the edit point** in the rendered request.
   - Cost: the same reasoning loss as option 1, and no beta needed for older turns.
   - Problem: it conflicts with §5's in-flight tool loop rule. The API requires the current
     turn's thinking blocks to be passed back, and they sit after the edit point too. So this
     option still needs option 1 whenever a loop is in flight.
3. **Only edit when no thinking block follows the edit point.**
   - Problem: Claude Code turns almost always contain thinking, so plans would almost never
     apply.
4. **Use the API's server-side compaction** (`compact-2026-09-04`, on-demand) instead of Liber-authored
   frozen blocks.
   - Upside: the docs say the checked prefix starts at the most recent compaction block.
   - Cost: Liber no longer chooses what is kept, which is its whole point. It would need a
     separate investigation.

## Decision
Option 1, applied only to requests Liber actually changes ("rendered" outcome): send the
`thinking-binding-controls-2026-08-01` beta and `thinking.block_binding.prefix_mismatch_behavior:
"drop_block"`. Requests with no plan stay byte-identical. Plus:

- **A1. Edit only at clean turn boundaries.** A plan is applied only when the request's latest
  message is a user turn that starts a new exchange (no `tool_result` blocks), so no tool loop is
  in flight and no mid-task reasoning is dropped. Otherwise the pending plan waits for the next
  clean boundary (§6). The docs say not to compact between a `tool_use` and its `tool_result`.
- **A2. Byte-identical rendering.** Rendering the same client history with the same plan
  version must produce identical bytes every time. Otherwise every request after a compaction
  would fail the check again and drop fresh reasoning. New Tier 1 metric: `render_determinism`
  (must be 1.0).
- **A3. Edge cases:**
  - Merge into the client's `anthropic-beta` header; never replace it. Keep any other
    `thinking` fields the client sent.
  - If the client already set `prefix_mismatch_behavior`, respect it. If it is `"error"`, forward
    the original request unedited and record why.
  - On Sonnet 5.5 and Haiku 5.5, `block_binding` requires `thinking.type: "adaptive"`. With any
    other thinking setting, strip every `thinking`/`redacted_thinking` block after the edit point
    instead (safe under A1, because no loop is in flight), or skip the edit if that isn't valid.
  - Unknown model: forward unedited until the rule is confirmed for it.
- **A4. Measure drops.** Record `input_transformations` from responses (streaming: the
  `message_start` event) per request. Tier 2 runs use `"error"` mode so any unintended
  binding break surfaces as a 400 with the failing path. `plan/cost.py` and the dashboard count
  the reasoning dropped by a compaction as part of its cost.
- **A5. Reasoning carry-forward experiment.** The summarizer gets an optional flag,
  `carry_forward_reasoning` (default off): when on, it reads the thinking text of the turns it
  summarizes (whatever the API returned, which may itself be a summary) and adds a short
  "earlier decisions and why" section to the frozen block, as plain text. Thinking blocks can't
  be re-inserted as thinking (signatures). Tier 2 compares four conditions (§12): full context,
  Liber with thinking dropped, Liber with carry-forward, and client auto-compaction. The flag
  stays off unless the data shows it helps more than it costs.

## Consequences
- Session 4's `render()` adds the beta header and `block_binding` field on edited requests,
  validated and logged per request; the break-point policy enforces A1.
- Each compaction drops the reasoning after the edit point once. After that, new reasoning is
  produced against Liber's rendered history and stays valid as long as A2 holds.
- Revisit when the beta goes GA or its name changes, and after the first Tier 2 results.
- v2 investigation: the API's own context editing (`clear_tool_uses_20250919`) and on-demand
  compaction keep thinking valid; a "compatible mode" for offload could use them.
