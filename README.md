# Liber

Liber is a local API proxy that lets you decide what stays in an LLM's context (keep,
summarize, offload, or delete individual items) instead of relying on the client's blind
auto-compaction. The design lives in [ARCHITECTURE.md](ARCHITECTURE.md); progress is logged in
[docs/PROGRESS.md](docs/PROGRESS.md).

**Status:** Session 3. Liber forwards every request to the Anthropic API, streams the answer
back as it arrives, and records each `POST /v1/messages` exchange (token usage, cache reads and
writes, latency) in a local SQLite database. It also recognises which conversation each
request belongs to (a tree of sessions and branches), splits it into items (one per content
block), and checks it against the API's rules. It doesn't edit anything yet: every request
still goes upstream byte for byte. A Tier 1 eval harness measures all of this offline. Edit
plans arrive in Session 4.

## Setup

Requires [uv](https://docs.astral.sh/uv/). uv installs the pinned Python (3.11) itself.

```bash
make setup        # uv sync: creates .venv from the locked dependencies
```

## Run it and point Claude Code at it

```bash
uv run liber serve          # listens on http://127.0.0.1:8484
```

In another terminal:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8484
claude
```

That's all. Claude Code keeps using its own credentials (an API key or your subscription
login): Liber passes the `x-api-key` / `authorization` headers through untouched and never
stores or logs them. To check it's working, look for `messages_request` lines in Liber's
output, or query the database:

```bash
sqlite3 ~/.liber/liber.db \
  "select created_at, model, status_code, input_tokens, cache_read_input_tokens,
          cache_creation_input_tokens, output_tokens, duration_ms from requests order by id desc limit 5"
```

Any client that speaks the Anthropic Messages API and lets you set a base URL works the same way
(for the Anthropic SDKs, `ANTHROPIC_BASE_URL` or `base_url=`).

### What Liber records

| Table | One row per |
|---|---|
| `requests` | `POST /v1/messages`: usage, latency, and what Liber did (`liber_outcome`, `session_id`, `known_messages`/`new_messages`, `violations`, `overhead_ms`) |
| `sessions`, `branches`, `chain_nodes` | conversation, path through it, message (by hash: [D7](ARCHITECTURE.md), [ADR 0005](docs/decisions/0005-conversation-identity-details.md)) |
| `items` | content block: role, kind, position, content hash, estimated tokens. Never the content itself. |
| `plan_versions`, `plans` | a session's edit-plan versions, and which is applied or pending ([ADR 0009](docs/decisions/0009-edit-plans-session-4a.md)) |
| `content_cache` | content hash → where an offloaded original is kept |

`liber_outcome` is `rendered` when Liber sent your session's plan applied, `passthrough` when it
sent your request unchanged (no plan, or an edit held back: `edit_skipped` says why), or
`fallback` when Liber stepped aside because something failed. To see fallbacks and why:

```bash
sqlite3 ~/.liber/liber.db \
  "select created_at, liber_error, violations from requests where liber_outcome = 'fallback' order by id desc limit 10"
```

### Editing a session's context

Until the dashboard exists, plans are made from the command line, against the same database
the proxy uses ([ADR 0009](docs/decisions/0009-edit-plans-session-4a.md)):

```bash
liber plan show                       # sessions, newest first
```

```bash
liber plan show --session SESSION     # its items (message, block, kind, tokens, id) and plan versions
```

```bash
liber plan create --session SESSION --summarize-through 9 --offload ITEM_ID --note "build log"
```

```bash
liber plan apply --session SESSION    # takes effect at the session's next new turn
```

```bash
liber plan undo --session SESSION     # back to the previous version, also at the next new turn
```

A plan takes effect only when a request starts a new turn, never in the middle of a tool loop.
**Current limit:** an edit is held back, and your request sent unchanged, while the request still
contains thinking blocks. Editing history before them needs the preserved-thinking opt-in from
the next session (4b). Offloaded originals are kept in `~/.liber/blobs/`.

## Configuration

Settings are environment variables prefixed with `LIBER_` (see `liber/config.py`).

| Variable | Default | Meaning |
|---|---|---|
| `LIBER_HOST` | `127.0.0.1` | Interface to bind. Keep it on loopback: anyone who can reach the port can spend your API key. |
| `LIBER_PORT` | `8484` | Port to listen on. `liber serve --port` overrides it. |
| `LIBER_UPSTREAM_BASE_URL` | `https://api.anthropic.com` | Where requests are forwarded. |
| `LIBER_DATA_DIR` | `~/.liber` | Holds `liber.db` (and, later, offloaded content). |
| `LIBER_RECORD_BODIES` | `true` | Store request/response bodies. Usage and latency are always stored. |
| `LIBER_UPSTREAM_READ_TIMEOUT` | `600` | Longest silence (seconds) tolerated while streaming. |
| `LIBER_WAL_CHECKPOINT_SECONDS` | `1.0` | How often a background task copies SQLite's write-ahead log into `liber.db`. Requests never do it ([ADR 0008](docs/decisions/0008-wal-checkpoints-in-the-background.md)). |
| `LIBER_LOG_LEVEL` | `INFO` | Log level. |

## Development

| Command | What it does |
|---|---|
| `make setup` | Install locked dependencies with uv |
| `make check` | Format check, lint, type check (mypy strict), tests, Tier 1 eval gate. Same as CI. |
| `make fmt` | Auto-format and auto-fix lint |
| `make test` | Tests only |
| `make eval` | Tier 1 offline evals: replay the session fixtures, write `evals/results/latest.json` |

### Tier 1 evals (the merge gate's numbers)

`make eval` replays every session in `tests/fixtures/sessions/` as the requests a client would
send, through Liber's model functions and its real request path, and writes:

- `evals/results/latest.json`: seven metrics (validity, round trip, fallbacks, token reduction,
  prefix stability, render determinism, p95 overhead), each with a direction and a tolerance.
  Definitions are in [ADR 0006](docs/decisions/0006-tier1-metric-definitions.md).
- `evals/results/latest.trace.jsonl`: one line per replayed request (outcome, validator result,
  token estimates, overhead), for diagnosing a failing metric.

`make check` runs it, then `scripts/eval_gate.py` compares the results with the committed
`evals/baseline.json` and fails if any metric got worse than its tolerance allows. Only the owner
records the baseline, with the Makefile's baseline target, in a dedicated PR that explains the
new numbers (§12). Until it exists, the gate only warns. To add a session, see
`tests/fixtures/README.md`.

`uv run python scripts/bench_request_path.py` times the request path beyond what the gate
checks: p95 and max of each Tier 1 replay run, and a synthetic ~1 MB, 401-message conversation.
It reports and doesn't gate.

Tests run fully offline. A fake upstream is plugged into the proxy's httpx client, plus a real
uvicorn server on localhost for the streaming-timing tests. No API key is needed.

How the work is organized (sessions, hooks, the reviewer agent, the eval gate) is described in
[CLAUDE.md](CLAUDE.md) and ARCHITECTURE.md §15. Design decisions not already in ARCHITECTURE.md
are recorded in [docs/decisions/](docs/decisions/).

## Privacy

- Liber binds to `127.0.0.1` by default and sends nothing anywhere except the upstream API the
  client already uses.
- Credential headers are forwarded but never written to the database or logs (tested).
- Request and response bodies (your conversations, which may include code and secrets) are
  stored locally in `~/.liber/liber.db` unless `LIBER_RECORD_BODIES=false`. Liber creates
  `~/.liber` readable by you only (mode 700). Delete the database to remove everything (a
  `liber purge` command is planned).
- Content you choose to edit is kept locally even with body recording off: offloaded originals
  in `~/.liber/blobs/` (files mode 600) and summary text in the plan versions.
