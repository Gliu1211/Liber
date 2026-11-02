"""Background worker: exact token counts, summaries and topic labels (ARCHITECTURE.md §9, D9, D10).

Arrives in Session 5. Runs as an asyncio task started from the app lifespan and never sits on
the request path (hard rule 4).
"""
