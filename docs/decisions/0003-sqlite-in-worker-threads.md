# 0003: SQLite through a sync engine in worker threads

- Status: Accepted
- Date: 2026-11-02

## Context
Session 2 put database work on the request path: every `POST /v1/messages` looks up its
conversation and registers new messages and items (§3 steps 2-3, D7). §3's latency budget is
"a few milliseconds", and the Session 2 acceptance test requires a p95 under 10 ms for steps 2-5
across a 100-message conversation (`tests/test_pipeline.py`).

The first version used SQLAlchemy's async engine over `aiosqlite` with ORM objects. Measured on
the growing 101-message fixture (51 requests), identification alone averaged about 9 ms, and the
whole of steps 2-5 had a p95 of 18 ms. Profiling showed SQLite itself was well under 1 ms. The
rest was per-statement overhead: every SQL statement went to aiosqlite's thread and back
(through greenlet), plus ORM loading and unit-of-work flushes.

## Options considered
1. **Keep aiosqlite, issue fewer statements.** Measured: identification about 3.7 ms. Better,
   but every remaining statement still pays the per-statement hop.
2. **Cache known chain nodes in memory** and write to the database in the background. Fast, but
   adds a second source of truth and ordering problems (the next request must see this one's
   nodes).
3. **Sync SQLAlchemy engine (stdlib `sqlite3`), one `asyncio.to_thread` hop per Store
   operation.** Identification's statements run as plain SQL on the transaction's connection.
   SQLite uses WAL journaling with `synchronous=NORMAL`.

## Decision
Option 3. `Store.run(work)` runs `work(db)` in one transaction on a worker thread.
`session/chain.py:find_and_register` issues three to five plain SQL statements: one SELECT
(deepest known node joined with its branch), then one INSERT or UPDATE per table. The `IN` list
of hashes is a single expanding bind parameter, so the compiled query is cached.

Related request-path changes made for the same budget:
- `Item` is a frozen dataclass. Pydantic validation of ~250 items per request was the largest
  share of `parse()`.
- `canonical_json` reuses one `JSONEncoder`.
- The thinking-block check tries object identity before comparing by content.

Result on the same benchmark: identification about 1.5 ms (2.3 ms including the thread hop).
Steps 2-5 together: p95 about 5.5-6.6 ms, mean about 4 ms.

## Consequences
- Two fewer dependencies (`aiosqlite`, `greenlet`). The Store's public methods are still
  `async`, so callers didn't change.
- SQLite allows one writer at a time. Concurrent writes (request records vs identification)
  wait on sqlite3's busy timeout (5 s default), which is ample for a local proxy.
- WAL adds `liber.db-wal` and `liber.db-shm` files next to the database. `synchronous=NORMAL`
  means a power loss (not a crash) can lose the last few commits. That's acceptable for records
  that rebuild themselves on the next request.
- Revisit if the budget tightens: option 2 is the next step, or skip identification entirely
  for sessions with no plan once Session 4 exists.
