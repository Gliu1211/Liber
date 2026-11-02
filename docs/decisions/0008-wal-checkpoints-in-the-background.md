# 0008: SQLite WAL checkpoints run in a background task, never in a request's commit

- Status: Proposed
- Date: 2026-11-02

## Context
ADR 0003 put SQLite in WAL mode: a commit appends pages to `liber.db-wal` instead of rewriting
the database file. Those pages are copied back into `liber.db` by a *checkpoint*, after which the
log starts over. By default (`wal_autocheckpoint=1000`) SQLite checkpoints inside whichever commit
takes the log past 1000 pages (~4 MB). One of those commits is identification's, on the request
path (§3 steps 2-5, `session/chain.py:identify` → `Store.run`).

Measured: identification writes ~13 pages per request, so each Tier 1 run (79 requests, ~1050
pages) crossed the threshold once, and that request took ~11-14 ms instead of ~5. With
`wal_autocheckpoint=0` that spike disappeared. Real use crosses it far more often: request
bodies are recorded by default (§11), and a 1 MB body is ~250 pages of log.

## Options considered
1. **Raise the threshold.** Fewer spikes, each one bigger.
2. **Turn automatic checkpoints off on every connection; checkpoint from a background task on a
   timer (chosen).**
3. **Turn them off only for the request path:** a second engine for background writes (request
   records), which keep automatic checkpoints. Two connection pools, and the log then only stays
   bounded while records are being written.
4. **Checkpoint after every N writes**, counted in `Store.run`. The Store would have to start
   asyncio tasks itself. A timer is simpler, and the log stays bounded by write rate × interval.
5. **`RESTART`/`TRUNCATE` checkpoints.** They wait for readers and writers. `PASSIVE` copies what
   it can without waiting for them, and readers and writers carry on while it runs.

## Decision
Option 2.
- `configure_sqlite` sets `PRAGMA wal_autocheckpoint=0` on every connection. The pool is shared,
  so any connection can end up serving a request.
- `Store.checkpoint()` runs `PRAGMA wal_checkpoint(PASSIVE)` on its own connection in a worker
  thread and returns SQLite's `(busy, pages in the log, pages copied)`. It runs outside a
  transaction, as SQLite requires; Python's `sqlite3` only opens one before a write.
- `checkpoint_regularly(store, every_seconds)` loops forever: sleep, checkpoint, and log
  failures and carry on. The app's lifespan starts it (`LIBER_WAL_CHECKPOINT_SECONDS`, default
  1 s) and cancels it at shutdown, before `Store.close()`. SQLite checkpoints and removes the log
  when the last connection closes.
- The Tier 1 harness (`tier1.run_request_path`) runs the same task at the same interval, so the
  gate measures the request path as the app runs it.

**Why the log stays bounded:** once a checkpoint has copied every page, the next commit writes
from the start of the log again (tested). So the log holds about one interval of writes. Its file
keeps its largest size on disk (SQLite reuses the space rather than shrinking it), and goes away
on shutdown.

**Does a background checkpoint slow a request it overlaps?** Measured by replaying Tier 1 with
the checkpointer running far more often than in production (GC disabled to isolate the effect):
p95 rose ~1 ms with a checkpoint every 20 ms and ~2 ms every 5 ms. At one per second, overlaps
are rare, and an overlap costs ~1-2 ms instead of the ~11 ms of a checkpoint inside the commit.

## Results
- Tier 1, request path set up like the app (`scripts/bench_request_path.py`, 10 runs): the
  regular ~11-14 ms request in every run is gone; median max per run 13.7 ms → 6.2 ms.
- **What still makes single requests slow:** Python's full (generation 2) garbage collections,
  20-120 ms depending on how many objects the process holds. Every request over ~13 ms in 20
  Tier 1 runs coincided with one. They happen in only some runs, so p95 doesn't see them, but
  `summary.max_overhead_ms` does. The harness keeps every run's results, so its collections
  are slow. `gc.freeze()` after startup removed them in a quick experiment (worst of 30 runs:
  46 ms → 13 ms). That's a separate decision, left as a follow-up.

## Consequences
- Another process that opens `liber.db` (the `sqlite3` shell, a future `liber` command) uses
  SQLite's defaults and may checkpoint on its own. That's harmless.
- If the background task stops, the log grows until the next restart, and nothing is lost. The
  loop catches every error except cancellation at shutdown.
- A long-running read would stop `PASSIVE` from copying everything, so the log couldn't start
  over. Liber has no long reads today; the dashboard (§10) should keep its reads short.
