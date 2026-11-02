"""The HTTP proxy: forward requests upstream and relay responses (ARCHITECTURE.md §3, D12).

- forward.py: the upstream httpx client, header passthrough, chunk-by-chunk relaying.
- sse.py: watch the relayed bytes to rebuild the assistant reply and its token usage.
"""
