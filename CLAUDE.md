# Liber — Agent entry point

Liber is a local API proxy that lets users choose what stays in an LLM's context. This file is short on purpose; follow the links for depth.

**Read before any code:** `ARCHITECTURE.md` (source of truth) and `docs/PROGRESS.md` (where the last session left off).

## Hard rules (also enforced by hooks and CI where possible)

1. **Never break the user's session.** Any Liber failure → forward the original request unmodified and log it.
2. **Never store or log credentials.** Pass `x-api-key` / `authorization` through untouched; scrub fixtures.
3. **Bind to 127.0.0.1** by default.
4. **No LLM or extra network calls on the request path.** Background worker only.
5. **Never auto-edit frozen blocks** (D5); only a major compaction (D6) rebuilds them.
6. **Never modify thinking blocks or the in-flight tool loop** (§5). The one exception: leaving out a leading run of stale thinking blocks (the oldest ones, produced before the session's plan version took effect) per ADR 0012. Removing that run whole doesn't change any block that remains.
7. **Tests and Tier 1 evals run offline.** No real API keys in tests or CI.
8. **Never edit `evals/baseline.json` to make a change pass.** Baseline updates are their own PR (§12).

## Workflow for every session (ARCHITECTURE.md §15)

1. **Orient:** read `docs/PROGRESS.md`, the sections your task names, and the existing code you'll touch.
2. **Plan:** for anything touching more than one module, write a short plan (files, interfaces, tests) before editing.
3. **Tests first:** turn the task's acceptance criteria into failing tests.
4. **Loop:** implement the smallest piece → `make check` → diagnose from the output → fix → repeat. A Stop hook runs `make check` and blocks finishing while it fails.
5. **Independent review:** run the `reviewer` subagent on your diff; fix real findings or explain why not.
6. **Hand off:** append an entry to `docs/PROGRESS.md` (done, decisions, open issues, next step). Record any new design decision as an ADR in `docs/decisions/`.
   - New ADRs are always **Proposed**. Only the owner changes an ADR's status.
   - Don't start or queue follow-up tasks. List proposed follow-ups in `docs/PROGRESS.md` for the owner to decide.
7. **PR:** one task per PR, conventional-commit title, description with a **"How it works"** walkthrough.

## Learning mode (the owner must be able to explain every line)

- Plain, readable code; no metaprogramming or framework magic beyond FastAPI/SQLAlchemy/pydantic basics.
- Each module starts with a docstring: why it exists and which ARCHITECTURE.md section it implements.
- Comment the *reason* for non-obvious lines, not the mechanics.

## Commands

- `make setup` — install with uv (locked dependencies)
- `make check` — format check, lint, type check, tests, Tier 1 eval gate (same as CI)
- `make fmt` — auto-format
- `make eval` / `make eval-baseline` — run Tier 1 evals / rewrite the baseline (baseline PRs only)
