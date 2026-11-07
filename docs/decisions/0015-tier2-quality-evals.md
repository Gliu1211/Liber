# 0015 — Tier 2 quality evals: shared history, Liber in-process, strict binding

- Status: Proposed
- Date: 2026-11-07

## Context
Session 7 builds §12's Tier 2: a manual eval against real models (`python -m liber.evals.tier2
--live`) comparing four conditions:
- A: full context;
- B: Liber, thinking dropped;
- C: Liber with `carry_forward_reasoning`;
- D: simulated client auto-compaction.

The brief asks for:
- configurable repeats, with mean and spread;
- a reasoning-dependent task;
- per-condition success, tokens, thinking dropped, cache reads and writes, and cost;
- every request sending `prefix_mismatch_behavior: "error"`, with breaks reported by failing path;
- needle faithfulness graded by an LLM judge;
- results in `evals/history/` plus a markdown report with the comparisons and trends.

Three things weren't settled:
- **How to compare conditions fairly when the model answers differently every run.**
- **How to send B and C with `"error"`.** The proxy sends a client's `"error"` request
  unedited (ADR 0012 decision 3), so going through it would measure nothing.
- **How to keep B's and C's summaries identical.**

## Decision
1. **One history per repeat, shared by all four conditions.**
   - The task's turns run once, with full context, thinking on and scripted tool output.
   - Each condition then continues that same history with the task's probe question and then its
     needle question. Differences between conditions come from context handling, not from
     different conversations.
   - The tasks compact turns `0..compact_through_turn` and leave at least one later turn, so B and
     C summarize a prefix and keep a live tail. D replaces everything, as a client's compaction
     does.
2. **B and C run Liber's request path in-process.**
   - `pipeline.prepare` runs on a throwaway database. The plan is made with
     `commands.create`/`apply`, exactly as the CLI and dashboard make it, and takes effect at the
     probe, a clean turn.
   - The bytes Liber would send then go straight to the API, with `drop_block` replaced by
     `"error"` (`with_strict_binding`).
   - Every request in every condition gets the same strict setting and the beta. Any thinking
     Liber failed to leave out becomes a 400. `binding_error` classifies it, and the report lists
     each by its `messages.N.content.M` path.
   - A B or C request Liber didn't send compacted is recorded as an error rather than passing
     silently as a copy of A.
3. **Identical summaries.**
   - The worker writes each repeat's summaries once, with notes, into one throwaway database.
   - The rows are copied into B's and C's databases.
   - B's plan leaves the notes out (`commands.create`, carry_forward off).
   - The frozen blocks then differ only by C's labelled notes section, which a test checks.
4. **D (simulated client compaction).**
   - One call asks the model to summarize the whole history (`COMPACT_PROMPT`).
   - The client then continues from a single user turn holding that summary and the probe.
   - That call's tokens are reported as D's overhead and included in its cost.
5. **Judge.**
   - The judge model (default `claude-opus-5-5`) grades the probe answer against the task's rubric
     (task success), and the needle answer against the planted fact.
   - It uses structured output, with the graded material sent as escaped JSON between tags, so an
     answer can't instruct the judge.
   - A missing verdict is `None` and is left out of rates.
6. **Metrics.**
   - Token counts come from each response's `usage`.
   - Thinking dropped counts `input_transformations` entries of type `thinking_dropped`.
   - Cost is input-side only, priced from `Settings.pricing`. The table has no output prices;
     output tokens are reported but not priced.
   - B/C's worker summary calls aren't metered: `ModelClient` returns no usage.
7. **Report** (`tier2_report.py`, plain functions over the history JSON).
   - Per task and condition: mean, min, max and standard deviation of every metric.
   - Comparisons: A vs B, C vs B, B vs D and C vs D, each as a success-rate difference and an
     input-token change, per task and averaged over tasks.
   - Trends: per-condition success and input tokens from every history file, ordered by date.
8. **Tasks** (`evals/tier2_tasks/*.json`, data the owner can edit).
   - `dead_end_debugging` is reasoning-dependent: the database is ruled out only in the model's
     thinking, and the visible replies name the real cause.
   - `migration_facts` tests whether a detail buried in a long tool result survives.

## Consequences
- **Not a full replay.** It doesn't replay real sessions: a scripted task and two questions per
  condition is a small sample, so the spread is reported. More tasks are the main lever.
- **The reasoning task depends on the model.** It works only if the model actually reasons about
  the database in its thinking and keeps it out of the visible reply. `display: "omitted"`
  thinking can't be carried forward (C = B then).
- **Cost understates B, C and D a little:** no output prices, and no metering of worker calls.
  An `output` price column and usage-returning worker calls are follow-ups.
- **Offline tests use a fake API that enforces binding.** A thinking signature is a hash of the
  history before the block. A test breaks Liber's stale-thinking removal on purpose and checks
  that the run reports the failing path.
- **Revisit when** the real API's `input_transformations` or binding error format differs from
  what `Usage.add` and `binding_error` assume (check on the first live run).
