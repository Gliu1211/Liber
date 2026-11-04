# 0012: Leave out stale thinking; `drop_block` only as a safety net

- Status: Accepted (owner, 2026-11-04)
- Date: 2026-11-04

## Context
ADR 0004 chose to send `thinking.block_binding.prefix_mismatch_behavior: "drop_block"` (with
the `thinking-binding-controls-2026-08-01` beta) on every request Liber edits. Its consequences
said "each compaction drops the reasoning after the edit point once; after that, new reasoning
is produced against Liber's rendered history and stays valid as long as A2 holds."

Checking the current API docs (2026-11-04) for Session 4b showed that assumption doesn't hold on
its own:
- With `"drop_block"`, the API drops **the first mismatched thinking block and every thinking
  block after it**.
- The client keeps every thinking block it ever received and resends them on every turn,
  including the old ones produced against its unedited history.
- So after a compaction the old blocks mismatch on every request, and the new reasoning that
  follows them would be dropped too. The model would lose its recent reasoning on every turn,
  not once per compaction.
- The docs also say removing a **leading run** of thinking blocks, oldest first, keeps every
  later block valid.

## Options considered
1. **`drop_block` alone** (ADR 0004 as written): the model loses its recent reasoning on every
   request after a compaction.
2. **Leave out the stale blocks, and keep `drop_block` as a safety net (chosen).**
3. **Don't edit sessions that use thinking** (4a's hold-back, ADR 0009): almost no Claude Code
   request could be edited.

## Decision
1. **Record when a version takes effect.** The request's message count is stored in
   `plans.activated_at` when a plan version takes effect, and again after an undo.
   - Every thinking block in that request is stale. A1 guarantees no tool loop is in flight
     there, so none of them is mid-task reasoning.
   - Every later request leaves out the thinking blocks in messages before that point.
   - They are always the oldest thinking blocks still in the request, a leading run. The
     validator's `thinking_unchanged` rule checks that what's sent is exactly the tail of the
     client's thinking.
   - Text and tool calls in those messages stay. If removing the thinking would empty a
     message, that version doesn't fit the request.
2. **The rule covers the empty plan too.** Once a version has taken effect in a session, the
   thinking produced under it was produced against Liber's rendering, not the client's history.
   So an undo back to no plan, a fork the plan doesn't fit, or a plan that fails all still leave
   out the stale run. A session that never had a plan is sent byte-identical, as before.
3. **The opt-in is a safety net on every edited request** (`plan/thinking.py`):
   - Liber adds `block_binding: {prefix_mismatch_behavior: "drop_block"}` and merges the beta
     into the client's `anthropic-beta` header without replacing it. Done only where the docs say
     the field is accepted: thinking type `"adaptive"` or `"enabled"`, or `thinking` omitted on
     models where that means adaptive.
   - A client that set the field keeps its choice. `"drop_block"` is kept; `"error"` (or an
     unknown value) means its own request is sent and the reason recorded.
   - `"between_tools"` and other settings take no other field, so those requests rely on
     decision 1 alone (ADR 0004 A3's "strip" path).
   - An unrecognised model gets the client's request unedited if thinking blocks remain (A3).
     If none remain, it is sent edited without the field.
   - If decision 1 is right, the API never has anything to drop, and every response's
     `input_transformations` (now recorded per request, A4) is empty. A non-empty one means a
     render bug or a model switch, and it's visible.
4. **The 4a hold-back is removed.** This supersedes ADR 0009 decision 4 ("edits held back while
   the rendered request has thinking blocks").
5. **The hard rule says so.** CLAUDE.md rule 6 and ARCHITECTURE.md §5 ("never modify thinking
   blocks") now name this as the one exception (owner, 2026-11-04). Removing a whole leading run
   changes no block that remains.

## Consequences
- **Lost reasoning per compaction.** Each compaction loses all the reasoning in the history
  once, not only what came after the edit point; most compactions start at the front anyway.
  `liber plan show` reports how much (`cost.py`: blocks and estimated tokens) before you apply.
- **Tier 1 `token_reduction`** went from 0.0075 to 0.31, because the thinking-heavy fixtures'
  edits are now sent. `prefix_stability` stays at 1.0, now over 43 minor compactions.
- **Remaining risk: fallbacks after an edit.** A request that falls back because something in
  Liber failed is sent byte-for-byte, as hard rule 1 requires. In a session Liber has edited,
  that request can carry thinking produced against Liber's rendering, which an enforced account
  rejects with a 400. Avoiding it would mean editing a request after Liber itself failed.
  Fallbacks are rare (fallback rate 0 on the fixtures); revisit if they show up in
  `liber_outcome = 'fallback'` rows for edited sessions.
- **Revisit when** the beta goes GA or changes name, or if the docs change what `drop_block`
  drops.
