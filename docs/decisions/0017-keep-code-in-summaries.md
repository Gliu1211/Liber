# 0017 — Keep the assistant's code word for word in summaries

- Status: Accepted (owner, 2026-11-09); amended (owner, 2026-11-09): code that exists only in the conversation
- Date: 2026-11-09

## Context
In the 2026-11-08 Tier 2 run (`evals/history/2026-11-08-222804.*`, 5 repeats), Liber's summaries
lost code:
- On `code_reconstruction`, B and C failed 0/5, while client auto-compaction (D) and full context
  (A) passed 5/5. The probe asks for `parse()` exactly as it stands after two edits.
- B's summary describes the edits ("returned the full updated parse() with an early check: if
  not line or not line.strip(), return None. The rest of parse() is unchanged") but can't hold the
  function. The answers said honestly that they couldn't reproduce it, and none faked it. The
  previous run's B 3/3 was luck.
- The summary call is capped at 120 words per exchange (`worker/summarize.py`), and it writes
  prose. D passes because its summary comes from the main model, with no cap.

Liber is aimed at Claude Code users, whose conversations are mostly code. A compaction that loses
the current version of the code being worked on is a real weakness, not an eval artefact.

Where code lives in a conversation:
- **The assistant's text replies:** fenced code blocks, as in `code_reconstruction` ("reply with
  the full updated parse()").
- **The assistant's tool calls:** in Claude Code, most code is written through tool calls, such as
  `Write`'s `content` and `Edit`'s `new_string`.
- **Tool results:** file reads, command output. This is external content.

## Options considered
1. **A bigger summary cap, and asking the summarizer to include code.** The code would be
   model-written: possibly paraphrased, abbreviated or subtly wrong, which is worse than missing,
   because the main model would trust it. It's also more Haiku output on every exchange.
2. **The summarizer chooses, Liber copies.** The summary call is shown the exchange's code
   candidates, each with an id, and returns the ids worth keeping. Liber copies those from the
   transcript, word for word, into the frozen block. The model decides *what*; the bytes never
   pass through it.
3. **The user pins code** (ADR 0016 already allows pinning text and `tool_use` items). It's exact,
   but manual, and it needs the user to know beforehand what they'll need. It's useful as a Tier 2
   control and as an override, not as the default.
4. **Keep the last version of each file or function automatically, without a model.** That needs
   Liber to understand code identity (which block is a newer version of which). A later
   refinement of option 2, not a start.

## Decision
Option 2, with option 3 as the Tier 2 control.

1. **Candidates** (`worker/summarize.py`): only code that exists just in the conversation, meaning
   **fenced code blocks in the assistant's text replies** (amended, see below).

   Each is given an id (`code-1`, `code-2`, …) and marked as a candidate in the transcript the
   summarizer sees, inside the quoted data.

   Not candidates:
   - **Code the assistant wrote to files** (`Write`'s `content`, `Edit`'s `new_string` and the
     like). It is on disk, and the model can read the file again, exactly and on demand.
   - **Tool results** (file reads, command output). They are external content (prompt-injection
     surface, ADR 0013/0016), and keeping them is the user's choice: a pin.
2. **The summary call** returns a new field `keep_code`: the ids worth keeping, meaning the code a
   later turn would need exactly, such as the current version of something being worked on.
   - The schema lists only the given ids, and the worker also drops any id it didn't offer before
     saving: structured output is a strong hint, not a guarantee.
   - The prompt says to prefer the latest version when an exchange revises the same code.
   - `PROMPT_VERSION` goes up, so old summaries aren't reused.
3. **Rendering** (`plan/apply.frozen_text`). The chosen code goes in a section "Code kept as is
   (written by the assistant earlier; copied word for word):".
   - Each block is labelled with where it came from, e.g. `[assistant code, message 5]`.
   - It is fenced as it was, not escaped; only Liber's own tags are neutralized, as for pins.
   - The bytes are the fenced block's own text, copied from the transcript.
   - An item the user also pinned (ADR 0016) is shown once, as the pin.
   - The text is fixed in the plan version, as summaries and pins are (ADR 0004 A2).
4. **Limits.**
   - At most 4,000 characters of kept code per exchange, whole blocks only, in the summarizer's
     order.
   - A single block over the limit is not cut. It is left out, with a one-line note in the section
     naming it and its size, so the reader knows code existed there. Pinning it (ADR 0016) or a
     later `recall` (D3) is the way to keep it.
   - Per summary range: at most 12,000 characters, newest exchanges first, since later code
     supersedes earlier. What doesn't fit gets the same note.
   - Kept code counts toward "a plan must make requests smaller" (§6). If it would make a plan
     refuse, the refusal message says kept code is why, and the user can summarize a longer range.
   - The numbers are starting points for Tier 2 to tune.
5. **What's stored.** Kept code is stored with the exchange's summary row (`exchange_summaries`),
   like the summary text. It is conversation content, under the same rules as summaries (ADR
   0013): stored even with `LIBER_RECORD_BODIES=false`, and deleted by `liber purge`.
6. **B and C stay comparable.** Kept code is part of the summary call's result, so it is identical
   with carry_forward_reasoning on and off. Only the notes, a separate call, differ (ADR 0004 A5).
   Like the summary, it is cached per exchange and summarizer, and is rewritten once when
   `PROMPT_VERSION` changes.
7. **Tier 2.**
   - Before building 1–5, run a control: condition E on `code_reconstruction`, pinning the
     assistant's last full `parse()` reply. This needs a task field for pinning an assistant turn's
     text, such as `keep_assistant_text_of_turns`.
   - E then shows what keeping the code is worth, and B after this change should approach E.
   - `code_reconstruction_reread` is the on-disk counterpart: the same edits, saved with
     `write_file` in a simulated workspace, and questions allowed to reread the file. It measures
     whether compaction still costs success when the code is on disk, and what the reread costs
     in tokens. This change should leave it unaffected.

## Amendment (owner, 2026-11-09): only code that exists just in the conversation
- **What changed:** candidates are now fenced code in the assistant's replies only. Code the
  assistant wrote to files through tool calls is no longer kept.
- **Why:** in a Claude Code session most code is written to disk, and the model can read a file
  again, exactly and cheaply, when it needs it. Keeping that code in the summary spends tokens on
  something the model can fetch. Code that lives only in the conversation is different: proposed
  patches, functions the user asked to see but never saved, earlier versions since overwritten.
  Nothing can fetch it back, so it's what a summary must keep. `code_reconstruction` is exactly
  this case: `parse()` was only ever printed in replies.
- **What it measures:** `code_reconstruction_reread` is the on-disk twin. If rereads recover
  success there at a modest token cost, the narrower scope is right.
- **Out of scope:** code the user pasted into their own messages also exists only in the
  conversation. It isn't a candidate in v1 (the user's text is summarized like any other text), and
  could be added the same way if Tier 2 shows a need.
- **Consequences:** fewer and smaller candidates, so the per-exchange and per-range limits bind
  less often. There is no longer a list of tool-call field names to maintain as clients change.

## Consequences
- **Code-heavy sessions keep their working code** at the cost of its tokens, chosen per exchange
  by the summarizer. Superseded versions may be kept in earlier exchanges, which wastes tokens but
  isn't wrong. The model reads the latest. Option 4 can trim them later.
- **The summarizer sees more structure:** candidate ids in the transcript and one more output
  field. Summary calls get slightly longer, and old summaries are rewritten once after the
  `PROMPT_VERSION` bump. ADR 0013 noted the same one-time worker cost.
- **Injection surface.**
  - Assistant-written code is lower risk than tool output, but not zero: an assistant can quote
    hostile text into a code block.
  - It is labelled as a record, as pins are, and a summarizer can't add bytes, only choose ids.
- **Revisit when** a model can recall offloaded items on demand (D3's v2 `recall`). Kept code
  could then become a pointer for large files.
