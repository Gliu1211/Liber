"""Tier 1 eval gate (ARCHITECTURE.md §12).

Compares the latest Tier 1 results to the committed baseline and exits non-zero if any
metric regresses beyond its tolerance. Results and baseline share one JSON format:

{
  "metrics": {
    "validity_rate":   {"value": 1.0,  "direction": "higher", "tolerance": 0.0},
    "token_reduction": {"value": 0.42, "direction": "higher", "tolerance": 0.02},
    "p95_overhead_ms": {"value": 3.1,  "direction": "lower",  "tolerance": 2.0}
  }
}

"direction" says which way is better; "tolerance" is the allowed absolute regression,
taken from the baseline (so loosening a tolerance also requires a baseline PR).
"""

import json
import sys
from pathlib import Path


def load(path: Path) -> dict[str, dict]:
    return json.loads(path.read_text())["metrics"]


def compare(latest: dict[str, dict], baseline: dict[str, dict]) -> list[str]:
    """Return a readable failure message for every regressed or missing metric."""
    failures = []
    for name, base in baseline.items():
        if name not in latest:
            failures.append(f"{name}: missing from latest results")
            continue
        new, old, tol = latest[name]["value"], base["value"], base.get("tolerance", 0.0)
        # A regression is movement in the "worse" direction by more than the tolerance.
        worse_by = (old - new) if base["direction"] == "higher" else (new - old)
        if worse_by > tol:
            failures.append(f"{name}: {old} -> {new} (worse by {worse_by:.4g}, tolerance {tol})")
    return failures


def main() -> int:
    latest_path, baseline_path = Path(sys.argv[1]), Path(sys.argv[2])
    if not latest_path.exists():
        print("eval-gate: no results yet (Tier 1 harness not built), skipping")
        return 0
    if not baseline_path.exists():
        print("eval-gate: WARNING no evals/baseline.json; the owner must run `make eval-baseline` and commit it")
        return 0
    failures = compare(load(latest_path), load(baseline_path))
    if failures:
        print("eval-gate: FAILED")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("eval-gate: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
