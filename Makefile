# Single definition of "done" (ARCHITECTURE.md §15). Agents, the owner, and CI all run `make check`.
.PHONY: setup fmt lint typecheck test eval eval-gate eval-baseline check

setup:
	uv sync --all-extras

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy liber

test:
	uv run pytest -q

# Tier 1 offline evals (§12). The harness arrives in Session 3; until then these targets are no-ops.
eval:
	@if [ -d liber/evals ] && [ -f liber/evals/tier1.py ]; then uv run python -m liber.evals.tier1 --out evals/results/latest.json; else echo "eval: Tier 1 harness not built yet, skipping"; fi

eval-gate: eval
	uv run python scripts/eval_gate.py evals/results/latest.json evals/baseline.json

# Owner-only: rewrites the baseline. Run in a dedicated PR and explain the new numbers.
eval-baseline: eval
	cp evals/results/latest.json evals/baseline.json
	@echo "Baseline updated. Commit it in its own PR with a justification."

check: lint typecheck test eval-gate
