# 0005: How D7's hash chain is pinned down (sessions, branches, items)

- Status: Accepted (implements D7, which is itself Provisional)
- Date: 2026-11-02

## Context
D7 says to hash messages into a chain, find the longest stored prefix, and treat divergences as
branches. Implementing it raised questions D7 leaves open: what exactly is hashed, what a
"session" is versus a "branch", what an item's id is, and how concurrent requests behave.

## Decisions
1. **What `h_0` covers:** `system` and `tools` only. The model and request settings aren't
   included, so a model switch mid-conversation stays the same conversation.
2. **What is ignored when hashing:** `cache_control` markers, but only where the API puts them
   (blocks, blocks inside a `tool_result`, tool definitions, system blocks). A tool *input* with
   a `cache_control` key is real data and is hashed. The `"content": "hi"` shorthand hashes like
   `[{"type": "text", "text": "hi"}]`. Keys are sorted.
3. **Canonical message:** the message's fields (role, plus extras such as `clear_at`) with
   `content` replaced by its blocks' content hashes. That's equivalent to hashing the full
   message, but each block is serialized once, which `parse()` needs for content hashes anyway.
4. **Branches and sessions:**
   - A *branch* is a path through the message tree; every request ends on exactly one branch.
   - A request that matches beyond its newest message extends that branch. One that leaves a
     branch part-way forks a new branch there (parent, fork depth).
   - A request that matches nothing past the root starts a new *session*.
   - So a session is the family of branches that share a first message.
   - New table `branches` (not in §8's initial list) holds parent, fork depth, head and last
     use. A session's last use is its newest branch's.
5. **Item ids:** the first 16 hex characters of the message's chain hash, then `-<block
   index>`. They're stable across requests, readable in logs and pointer notes, and the 64-bit
   prefix is plenty for one user's local data. Items belong to messages, so branches sharing a
   prefix share those items. A per-session plan operation on such an item applies to every
   branch that contains it, which is correct: it is literally the same content at the same
   point.
6. **Concurrency:** identification runs under an `asyncio.Lock` (one conversation update at a
   time, in this process) inside a `BEGIN IMMEDIATE` transaction (atomic across processes too).
   Both waits are short, and a busy database means the request goes through untouched (ADR
   0003). Node and item inserts are `ON CONFLICT DO NOTHING`; they're content-addressed, so a
   duplicate is the same row.
7. **Nothing changed → original bytes:** with an empty plan, the pipeline forwards the client's
   exact bytes, not a re-serialization. It still renders and compares, and a mismatch is
   recorded as a parse/render bug (fallback).

## Concerns (D7 is Provisional)
- Claude Code's system prompt or tool list can change mid-conversation (date rollover, MCP
  servers connecting). That changes `h_0` and starts a new session. If that proves noisy in
  dogfooding, options are to leave `tools` out of `h_0`, or to use
  `metadata.user_id` (which Claude Code fills with a session id) as a hint.
- Two unrelated chats that open with the same words under the same system prompt end up as
  branches of one session. That's harmless for plans (see 5), but the dashboard should show
  branches clearly (§16 open question).
