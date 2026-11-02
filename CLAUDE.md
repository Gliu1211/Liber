# Liber — Instructions for every session

Read `ARCHITECTURE.md` fully before writing code. It is the source of truth. Decisions marked **Provisional** may be challenged in the PR description, but implement them unless they're clearly wrong.

## Hard rules

1. **Never break the user's session.** If any Liber step fails (parse, render, validate), forward the original request unmodified and log the failure.
2. **Never store or log credentials.** Pass `x-api-key` / `authorization` through untouched. Scrub them from fixtures and logs.
3. **Bind to 127.0.0.1** by default.
4. **No LLM or extra network calls on the request path.** Summaries, topics, and exact token counts happen in the background worker.
5. **Never auto-edit frozen blocks** (D5). Only a major compaction (D6) may rebuild them.
6. **Never modify thinking blocks or the in-flight tool loop** (§5).
7. **Tests run offline** against recorded fixtures. No real API keys in CI.

## Learning mode (the owner must be able to explain every line in an interview)

- Prefer plain, readable code over clever abstractions. No metaprogramming, no framework magic beyond FastAPI/SQLAlchemy/pydantic basics.
- Every module starts with a docstring explaining **why** it exists and which ARCHITECTURE.md section/decision it implements.
- Non-obvious lines get a short comment explaining the reason, not the mechanics.
- Every PR description includes a **"How it works"** walkthrough: the data flow through the new code in plain English, plus any tradeoffs you made.

## Conventions

- Python 3.11+, async throughout. FastAPI, httpx, pydantic v2, SQLAlchemy 2.0 (SQLite), structlog, pytest + pytest-asyncio, ruff.
- Layout per ARCHITECTURE.md §13. Stay inside the modules your task names; keep changes to shared models minimal and call them out in the PR.
- Done = tests pass, ruff clean, PR with the "How it works" section and a list of anything stubbed or provisional.
