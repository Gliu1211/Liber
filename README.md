# Liber

Liber is a local API proxy that lets you decide what stays in an LLM's context (keep,
summarize, offload, or delete individual items) instead of relying on the client's blind
auto-compaction. The design lives in [ARCHITECTURE.md](ARCHITECTURE.md); progress is logged in
[docs/PROGRESS.md](docs/PROGRESS.md).

**Status:** Session 1, a transparent passthrough proxy. Liber forwards every request to the
Anthropic API unchanged, streams the answer back as it arrives, and records each
`POST /v1/messages` exchange (token usage, cache reads and writes, latency) in a local SQLite
database. Context editing arrives in later sessions.

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
| `LIBER_LOG_LEVEL` | `INFO` | Log level. |

## Development

| Command | What it does |
|---|---|
| `make setup` | Install locked dependencies with uv |
| `make check` | Format check, lint, type check (mypy strict), tests, Tier 1 eval gate. Same as CI. |
| `make fmt` | Auto-format and auto-fix lint |
| `make test` | Tests only |
| `make eval` | Tier 1 offline evals (from Session 3) |

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
