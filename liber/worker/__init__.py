"""Background worker: exact token counts, summaries and topic labels (ARCHITECTURE.md §9, D9, D10).

An asyncio task started from the app lifespan (run.py), only when LIBER_WORKER_API_KEY is set. It
never sits on the request path (hard rule 4); see docs/decisions/0013.
"""
