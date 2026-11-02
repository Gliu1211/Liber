# 0004: Preserved thinking vs. Liber's history edits

- Status: **Proposed. The owner needs to decide before Session 4 (plans) starts.**
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
4. **Use the API's server-side compaction** (`compact-2026-01-12`) instead of Liber-authored
   frozen blocks.
   - Upside: the docs say the checked prefix starts at the most recent compaction block.
   - Cost: Liber no longer chooses what is kept, which is its whole point. It would need a
     separate investigation.

## Recommendation
Option 1, applied only to requests Liber actually changes ("rendered" outcome), plus:
- record `input_transformations` from responses (a Session 4 recording change), so drops are
  measured, not guessed;
- extend the validator: when the rendered prefix before a kept thinking block differs from the
  client's, the request must carry `drop_block` (otherwise it falls back to the original);
- treat the reasoning loss as part of a compaction's cost in `plan/cost.py` and the dashboard.

## Consequences (if accepted)
- Session 4's `render()` adds the beta header and `block_binding` field on edited requests.
  That's a deliberate, validated modification, logged per request.
- Requests through Liber with no plan stay byte-identical, so there's no effect on users who
  never edit.
- Revisit when the beta goes GA or its name changes; `liber/model/validate.py` documents the
  rule.
