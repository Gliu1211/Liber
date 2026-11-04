"""Metric records and the results file format shared with scripts/eval_gate.py (ARCHITECTURE.md §12).

Results and the committed baseline use one JSON format:

    {"metrics": {"validity_rate": {"value": 1.0, "direction": "higher", "tolerance": 0.0,
                                   "description": "..."}, ...},
     "summary": {...}}

"direction" says which way is better; "tolerance" is how far a value may move the wrong way
before the gate fails. The gate reads tolerances from the *baseline*, so loosening one is a
baseline change, reviewed like any other.
"""

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Metric:
    value: float
    direction: str  # "higher" or "lower" is better
    tolerance: float  # allowed movement in the worse direction
    description: str


def ratio(passed: int, total: int) -> float:
    """passed / total, with an empty set counting as fully passed."""
    return round(passed / total, 6) if total else 1.0


def p95(values: list[float]) -> float:
    """The 95th percentile by the nearest-rank method: the value 95% of samples are at or below."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))  # 1-based
    return ordered[rank - 1]


def write_results(path: Path, metrics: dict[str, Metric], summary: dict[str, Any]) -> None:
    """Write the results file (stable key order, so repeated runs give the same text)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"metrics": {name: asdict(metric) for name, metric in metrics.items()}, "summary": summary}
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def write_trace(path: Path, records: list[dict[str, Any]]) -> None:
    """One JSON object per line: what happened to each replayed request, for diagnosis (§12)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, sort_keys=True, ensure_ascii=False) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
