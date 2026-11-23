# 0018 — Ignore the client's billing header in conversation identity; fold side requests

- Status: Proposed
- Date: 2026-11-23
- Amends: [ADR 0005](0005-conversation-identity-details.md) (what h_0 covers). D7 is Provisional,
  and ADR 0005's Concerns asked for exactly this kind of dogfooding evidence.

## Context
Dogfooding with Claude Code turned up two problems in conversation identity (D7):

1. **A resumed chat became a new session, and its plan was lost.**
   - A chat was used on 2026-11-19 (session `7ac45c60`, plan version 1 applied) and resumed on
     2026-11-23. Liber registered it as a new session (`d909c545`).
   - A read-only comparison of the two requests found a single difference: the first system
     block, `x-anthropic-billing-header: cc_version=2.1.325.157; cc_entrypoint=cli; …`, had
     become `cc_version=2.1.326.2f5`. Claude Code had updated itself in between.
   - Tools, `metadata.user_id` and all 90 earlier messages were identical; the old session's
     messages were an exact prefix of the new request's.
   - h_0 covers the system prompt, and every chain hash builds on h_0. So the version string
     changed every message hash and item id, and the applied plan pointed at nothing.
2. **The session list filled with branches.** Session `7ac45c60` forked a new branch almost every
   turn. Every fork's replaced message was Claude Code's recap prompt ("The user stepped away and
   is coming back. Recap in under 40 words…"). When you come back after a break, Claude Code sends
   the conversation plus that prompt as a side request; your real next message then forks at the
   same point. Nothing broke, since plans apply to the shared history, but the list was hard to
   read.

## Options considered
For (1):
- **Match sessions by `metadata.user_id`** (ADR 0005's suggested hint). It stayed the same across
  the resume. But item ids would still change with h_0, so plans would need re-keying by content
  as well. More machinery, and it depends on a client-specific field's meaning.
- **Leave the system prompt out of h_0.** Too broad: two different agents (different system
  prompts) opening with the same words would merge.
- **Ignore only the billing-header line.** It describes the client (version, entry point), not
  the conversation. With it out of h_0, a resumed chat hashes exactly as before: same session,
  branch, item ids and plan. Chosen.

For (2):
- **Recognize Claude Code's recap prompt by its text.** Brittle, and specific to one client.
- **Fold any branch whose last message was replaced.** Its newest message has no continuation,
  and a later message was sent with the same parent. That covers the recap and also a one-message
  rewind, which is equally finished. A branch that diverged further back is a real alternative and
  stays listed. Chosen.

## Decision
1. **Identity** (`session/chain.py`): when hashing h_0, a system text block that starts with
   `x-anthropic-billing-header:` loses its first line. Nothing else changes, and the request is
   still forwarded byte for byte; this only affects what Liber hashes.
   - The header anywhere else, and any other change to the system prompt or tools, still makes a
     different conversation.
   - Switching between the CLI and the IDE (`cc_entrypoint`) is the same conversation too.
2. **Dashboard** (`dashboard/views.side_branches`): the session list shows such branches in a
   collapsed "side requests (last message replaced)" list under the session, not as forks. They are
   still stored and still reachable; only the listing changes.

## Consequences
- **A chat resumed after a client update keeps its session, item ids and plans.**
- **One-time re-registration.** Sessions stored before this change hashed the header, so the next
  request of each existing chat starts a new session once. That only affects data recorded before
  the change, which today is the owner's test data.
- **Other clients are unaffected.** A system prompt without the header hashes as before.
- **Revisit if** Claude Code moves volatile data elsewhere (another header, the first user
  message), or if another client turns out to put per-version text in its system prompt. The
  same normalization would apply there.
