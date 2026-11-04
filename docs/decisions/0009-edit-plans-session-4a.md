# 0009: Edit plans in Session 4a: versions, rendering, and what is held back until 4b

- Status: Proposed
- Date: 2026-11-04

## Context
Session 4 (edit plans, frozen blocks, break points; ARCHITECTURE.md §4-§7, D5, D6, D8, ADR 0004)
was split by the owner into two PRs:
- **4a (this ADR):** plans and their versions, applying them when rendering, the blob store and
  content cache, the `liber plan` CLI, and Tier 1 standard plans.
- **4b:** the preserved-thinking opt-in on edited requests (ADR 0004: beta header,
  `prefix_mismatch_behavior: "drop_block"`, the A3 edge cases), `input_transformations`
  recording (A4), the idle and threshold break points, major vs minor classification, and
  `plan/cost.py`.

That split raises one safety question. On current models, editing history before a thinking
block makes the API reject the request unless the client opts into dropping such blocks
(ADR 0004), and that opt-in only arrives in 4b. Sending such an edit in 4a would break hard
rule 1.

## Decisions
1. **Plans are versions of operations, per session** (`plan/versions.py`, tables `plan_versions`
   and `plans`).
   - Versions are numbered and immutable. Each records its parent, the version it was built
     on; version 0 is the empty plan and isn't stored.
   - The `plans` row holds the applied version, a pending version, and whether the user asked
     for the pending one to be applied.
   - Undo makes the applied version's parent pending, with apply requested. Undo therefore goes
     through the same path as applying a version and comes back to exactly the earlier render
     (tested).
   - Per session, as §4 says: items keep their ids across branches that share a prefix. A plan
     that names items a branch doesn't have doesn't fit it (decision 3).
2. **When a version takes effect** (`plan/policy.py`): a requested pending version is tried
   first, but only on a request that ends at a clean turn boundary (ADR 0004 A1). The request
   path promotes it if it fits and the result passes the validator. Otherwise the applied
   version is used, and if that doesn't fit either, the empty plan. Once applied, a version is
   used on every request, including mid-loop ones, so the rendered history never flips between
   versions. 4a implements break point 1, the user asking (`liber plan apply`); idle and
   threshold come in 4b.
3. **Applying a plan** (`plan/apply.py`, called from `render()`):
   - **Summarize:** a range of whole exchanges, starting where the live region starts and
     ending just before a user message that starts an exchange (§5), becomes a frozen block.
     Frozen blocks are text blocks at the front of the first live user message (D8). A further
     summary appends a block (D5).
     - The block reads `<liber:summary block="N" covers="msgs a-b">…</liber:summary>`, with
       an ASCII hyphen so the bytes stay ASCII.
     - Its text is fixed when the version is created and stored in the operation (`summary`),
       so rendering never computes it (A2).
     - The text is a stub until the worker writes summaries (Session 5). There is no separate
       `frozen_blocks` table: the plan version already holds the blocks, immutably.
   - **Offload:** the content becomes a pointer note: `[Liber: offloaded item <hash12> —
     "<label>", N tokens. Stored locally; restore from the Liber dashboard.]`.
     - A tool_result keeps its block, `tool_use_id` and other fields; only `content` changes.
     - Text, image and document blocks become a text note and keep the client's
       `cache_control` marker.
     - Tool calls, thinking and unknown blocks can't be offloaded.
   - **Delete:** a tool call takes its result with it and the other way round. A message left
     empty takes its whole exchange with it. Thinking blocks are never deleted on their own.
   - **What plans never touch:** the latest exchange, the turn in progress. This is stricter
     than §5's in-flight loop, which for Claude Code is the same exchange.
   - **Superseded operations:** operations on items a summary covers have no effect.
   - **A plan that doesn't fit:** it names items the request doesn't have, or a summary isn't
     at an exchange boundary. It raises `PlanNotApplicable`, the next candidate version is
     used, and `edit_skipped` records why. That is not counted as a fallback.
   - **No new cache breakpoint at the frozen boundary yet.** §5 suggests adding one when
     there's room, but whether there's room depends on how many markers the client sent, which
     can vary per request. Adding it only sometimes would change the frozen prefix's bytes and
     break A2. Left as an open question.
4. **Held back until 4b:** if the rendered request still contains thinking or
   redacted_thinking blocks, Liber sends the client's request unedited. The outcome is
   `passthrough`, with `edit_skipped` explaining. Edits whose summaries removed every thinking
   block (and requests without thinking at all) are sent. 4b replaces this rule with the
   `drop_block` opt-in.
5. **Offloaded originals** go to `<data dir>/blobs/<hash[:2]>/<hash>`. That is the item's
   canonical JSON, whose SHA-256 is its `content_hash`.
   - Files are mode 0600, written via a temporary file and a rename.
   - Writes happen in background tasks after the request, never on the request path.
   - The `content_cache` table records each blob's path. Its tokens, summary and topic columns
     wait for the worker.
   - Stored even while an edit is held back, so the original is there when 4b sends it.
6. **The plan state is read in identification's transaction** (`Identity.plan`), not in a
   separate database trip, to stay within §3's budget.

## Consequences
- **What 4a reduces today** (`token_reduction` 0.75% on the fixtures): only requests without
  thinking, or whose thinking was all summarized away, are sent edited. Claude Code requests
  almost always contain thinking, so 4a changes little in real use until 4b.
- **Content in the database:** plan versions hold summary text and offload labels, and blobs
  hold offloaded content, even with `record_bodies` off. This is content the user chose to
  edit; ARCHITECTURE §11's `liber purge` must delete it too.
- **Stub summaries can make tiny exchanges longer** (Tier 1's chat fixtures send more tokens,
  not fewer). 4b's cost model should refuse compactions that don't pay.
- **The CLI and the proxy share the database.** A CLI write takes the write lock briefly; the
  request path's 100 ms busy timeout covers it.
