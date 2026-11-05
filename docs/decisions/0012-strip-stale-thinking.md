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
   - **Which requests continue that point:** it is stored with the chain hash of the activating
     request's last message (`plans.activated_hash`). A request whose chain hash at that position
     matches continues the same history, and leaves out the thinking before it.
   - **Any other request** (another branch, an older request resent, or a version applied in 4a
     before activation points were recorded) leaves out *all* thinking before the tool loop in
     flight. Liber can't tell which rendering that thinking was produced against, and leaving
     out a leading run is always valid. The loop's own thinking is kept: it was produced against
     exactly this rendering, because the cut falls in the same place on every request of the
     loop. This costs reasoning on forks, and is never invalid.
   - **Sessions applied in 4a** get their activation point recorded at their next clean turn
     that goes out edited.
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
- **Branches and idle time.** Forks of an edited session lose their completed-turn reasoning on
  each new turn. The idle break point measures from the deepest matched branch's last request.
- **Fallbacks after an edit carry the opt-in** (owner-approved amendment to hard rule 1,
  2026-11-05). A request that falls back because something in Liber failed, or because a render
  failed the validator, keeps the client's messages unchanged. In a session Liber has already
  edited, it gets the `drop_block` opt-in (beta merged into `anthropic-beta`,
  `thinking.block_binding` added) under three conditions:
  - the model is confirmed;
  - the client hasn't set `prefix_mismatch_behavior` itself;
  - the API accepts the field for its thinking setting.

  Thinking produced against Liber's rendering is then dropped by the API (with every thinking
  block after it) instead of failing the request. The session continues and loses that
  reasoning. The opt-in is added from a fresh decode of the client's bytes, and if adding it
  fails, the bytes go as they came (`pipeline.add_fallback_opt_in`).
- **Remaining risk: the client's own request after an edit, without the opt-in.** In each case
  below Liber sends the client's request as it came, and in a session Liber has already edited
  it can carry thinking produced against Liber's rendering, which an enforced account rejects
  with a 400:
  - the client set `prefix_mismatch_behavior: "error"` (its choice);
  - the conversation moves to a model Liber hasn't confirmed;
  - the thinking setting takes no other field (`"between_tools"`, or older models without
    adaptive thinking);
  - Liber fails before it knows the conversation (an undecodable body, or the database too busy
    to identify it), so it can't tell the session was edited.
- **Revisit when** the beta goes GA or changes name, or if the docs change what `drop_block`
  drops.
