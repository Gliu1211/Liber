# 0001: Relay raw bytes; ask the upstream for an uncompressed body

- Status: Accepted
- Date: 2026-11-02

## Context
ARCHITECTURE.md §3 says the proxy passes every SSE event straight back as it arrives *and*
records the reply while streaming (step 7). The proxy must be invisible to the client (Session 1).

If the client sends `accept-encoding: gzip, br` and that header is forwarded, the upstream may
compress its response. The bytes Liber relays would then be compressed, and Liber couldn't read
them without decompressing a copy (gzip, brotli and zstd each need their own decoder).

## Options considered
1. **Forward `accept-encoding`, relay raw bytes, decompress a copy for recording.** Fully
   faithful, but needs a streaming decoder per encoding (extra dependencies for br/zstd) and
   more code on the hot path.
2. **Let httpx decompress, send decoded bytes to the client.** Means stripping
   `content-encoding` and rewriting `content-length`, so the client no longer gets the
   upstream's bytes.
3. **Send `accept-encoding: identity` upstream, relay raw bytes and headers unchanged.**

## Decision
Option 3. `forward.upstream_request_headers` replaces the client's `accept-encoding` with
`identity`; `relay` yields `httpx.Response.aiter_raw()` chunks, so the client gets exactly the
bytes and headers the upstream sent (minus hop-by-hop headers). Streaming and non-streaming
responses take the same path: both are just byte streams.

## Consequences
- Client↔Liber traffic is uncompressed, which costs nothing on localhost. Liber↔upstream JSON
  bodies are slightly larger; SSE streams are normally sent uncompressed anyway.
- If an upstream ignores `identity` and compresses anyway, the bytes are still relayed
  faithfully (with their `content-encoding` header). `ReplyCapture` then skips parsing and
  records a note instead of usage numbers.
- Revisit if Liber is ever used against a remote upstream over a slow link, where compression
  matters. Option 1 is the upgrade path.
