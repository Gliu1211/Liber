# 0007: parse() reuses the work done for the previous request of a conversation

- Status: Proposed
- Date: 2026-11-02

## Context
§3's budget: steps 2-5 add only a few milliseconds. Tier 1 measured a p95 of ~6-8 ms against
the 10 ms gate (ADR 0006), and the cost grows with the conversation. A client like Claude Code
resends the whole history on every turn, and `parse()` canonicalized and SHA-256-hashed every
content block of it every time, although only the last message or two are new.

Measured per request, before this change (this machine; timings vary ±20% between runs):

| | 101-message fixture (~90 KB), mean | synthetic 401 messages (~1 MB), mean |
|---|---|---|
| `json.loads` ×2 (request + validator's copy) | 0.36 ms | 5.3 ms |
| `parse()` | 1.7 ms | 12 ms (canonical JSON 4.6, SHA-256 1.1, message hashes 1.1, items 1.1) |
| `identify()` | 2.6 ms | 4.1 ms |
| `validate()` + round-trip compare | 0.34 ms | 1.9 ms |

Comparing all 401 decoded messages with the previous request's using `==` took **0.2 ms**,
against ~6 ms to canonicalize and hash them.

## Options considered
1. **Reuse the previous request of the same conversation (chosen).** Compare each message with
   the one at the same position in the previous request; if it's the same message after the
   same history, copy its chain hash, block hashes and token estimates.
2. **Option 1, and reuse the `Item` objects whole.** `Item.block` points at this request's
   block object (render needs it), so items are rebuilt for reused messages (~1.5 µs per block).
   Dropping `block` from `Item` and having render read blocks from the message frames would save
   another ~1-2 ms per MB. Not done: it changes the context model that Session 4 builds on, for
   a gain that matters only for very large requests.
3. **Compare raw bytes** of the old and new request bodies. Exact and fast, but finding where
   each message starts in the bytes needs a hand-written JSON scanner. Rejected.
4. **A content-addressed memo** (block → hash). Looking a block up needs a key, which means
   hashing it: no saving.

## Decision
Option 1.

**When a message's results are reused** (`model/anthropic.py:reusable_frame`): message `i` of
the request gets the previous request's results for message `i` when
- the chain hash *before* it is the same (h_{i-1} covers everything before message `i`, so the
  history is the same), and
- the message is the same. Plain `==` on the decoded JSON runs in C and, for JSON without
  numbers, means exactly "same canonical form". With numbers it doesn't: Python says
  `1 == 1.0 == True`, but canonical JSON writes them differently (and the model sees different
  tokens). So each message records `has_numbers` when it is first hashed (a walk over new
  messages only), and a message with numbers is compared with `chain.same_json`, a Python walk
  that also checks types. Typical messages with numbers (tool calls with a `limit`, results with
  `is_error`) have few JSON nodes, so the walk is cheap even when they carry long strings.

Because of these two checks, `parse(r, previous=p)` gives exactly what `parse(r)` gives, for any
`p`. Hypothesis tests this on arbitrary pairs of requests and on "next request" pairs (history
cut anywhere, then new messages), and checks that the frames and items point at the new request's
own objects. A cache marker moving off the old last message makes that one message unequal, so it
is rehashed. Its chain hash comes out the same (markers aren't hashed), so reuse goes on after it.

**What is always recomputed:** the root hash (system + tools: their size doesn't grow with the
conversation, and Claude Code's tool schemas are full of booleans, so `==` wouldn't do), the
token estimates and hashes of new or changed messages, and the `Item`s (option 2).

**Which previous request** (`RecentRequests`): the latest parsed request of each of the last 8
conversations, most recently used first, kept per process (`app.state.recent_requests`). `find()`
returns the newest entry with the same first message, system and tools (cheap `==` checks that
usually stop at the first difference); `remember()` puts the new request in its place. A wrong
pick costs only reuse, never correctness, because `parse()` checks everything it reuses. In
`pipeline.prepare`, `find`, `parse` and `remember` run with no `await` between them, so
concurrent requests can't interleave there. A failure in any of them is caught like any other:
the client's bytes are forwarded (hard rule 1, tested).

**What this relies on:** `previous.original` still holds what its hashes were computed from.
`parse()` and `render()` never modify the request (tested since Session 2). If something did, the
comparison would almost always fail and the message would simply be hashed again.

## Results
Same machine, same protocol before and after: `scripts/bench_request_path.py` (request path set
up like the app, results not retained; "before" ran it without the two lines that create the
cache and the checkpoint task, which didn't exist yet) and `make eval` (the gate's numbers).

| | before | after |
|---|---|---|
| `parse()` on the 101-message fixture's last turn (best of 30) | 2.8 ms | 0.85 ms |
| `parse()` on the ~1 MB request's last turn (best of 30) | 14.7 ms | 3.5 ms |
| Tier 1 replay p95 (bench, median of 10 runs) | 7.0 ms | 5.5 ms |
| Tier 1 `p95_overhead_ms` (`make eval`, median of 5) | 7.0 ms | see PROGRESS / PR |
| ~1 MB request, p95 over the last 20 turns (3 runs) | 35-44 ms | 22-29 ms |
| ~1 MB request, mean over the last 20 turns (3 runs) | 29-32 ms | 16-20 ms |

All Tier 1 metrics other than timing are unchanged, and the per-request trace (outcomes,
known/new messages, `rendered_sha256`, token estimates) is identical apart from `overhead_ms`.

## Consequences
- **Memory:** up to 8 decoded requests stay alive (one per conversation). A 1 MB request is a
  few MB of Python objects.
- **Still linear in the request size, but cheap per message:** the `==` comparisons (0.2 ms/MB),
  rebuilding frames and items (~2-3 ms/MB). Outside `parse()`: the two `json.loads`
  (~2.5 ms/MB each), `validate()` (~1.8 ms/MB), and `identify()`'s lookup of every chain hash
  (~1 ms/MB). At ~1 MB the request path is still ~17 ms on average, over the 10 ms budget for
  requests that large. Measured next steps, in order of payoff:
  1. `identify()`'s fixed cost: ~1 ms of its ~2.3 ms is SQLAlchemy building and cache-keying the
     `deepest_known_node` SELECT on every call; SQLite itself takes ~0.25 ms. Build it once.
  2. Look up only the chain hashes from the first message that wasn't reused (the earlier ones
     were registered by the previous request), falling back to all of them if none matches.
  3. The validator's separate copy of the request: decode once and copy only the new messages.
  4. Option 2 above.
- More long-lived objects make Python's full (generation 2) garbage collections slower. Those
  collections are now the main source of single slow requests (see ADR 0008).
