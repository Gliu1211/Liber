"""scripts/eval_gate.py: the Tier 1 gate fails when a metric regresses (ARCHITECTURE.md §12).

The gate is run exactly as `make eval-gate` runs it: as a script, judged by its exit code.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from liber.evals import tier1
from tests.helpers import FIXTURES

GATE = Path(__file__).resolve().parents[1] / "scripts" / "eval_gate.py"


def write(path: Path, metrics: dict[str, dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"metrics": metrics}))
    return path


def run_gate(latest: Path, baseline: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), str(latest), str(baseline)], capture_output=True, text=True, check=False
    )


BASELINE = {
    "validity_rate": {"value": 1.0, "direction": "higher", "tolerance": 0.0},
    "fallback_rate": {"value": 0.0, "direction": "lower", "tolerance": 0.0},
    "token_reduction": {"value": 0.40, "direction": "higher", "tolerance": 0.02},
    "p95_overhead_ms": {"value": 6.0, "direction": "lower", "tolerance": 4.0},
}


def with_value(name: str, value: float) -> dict[str, dict[str, Any]]:
    metrics = json.loads(json.dumps(BASELINE))
    metrics[name]["value"] = value
    return metrics


def test_passes_when_nothing_regressed(tmp_path: Path) -> None:
    baseline = write(tmp_path / "baseline.json", BASELINE)
    latest = write(tmp_path / "latest.json", BASELINE)

    result = run_gate(latest, baseline)

    assert result.returncode == 0, result.stdout
    assert "passed" in result.stdout


def test_fails_when_validity_is_deliberately_regressed(tmp_path: Path) -> None:
    baseline = write(tmp_path / "baseline.json", BASELINE)
    latest = write(tmp_path / "latest.json", with_value("validity_rate", 0.98))

    result = run_gate(latest, baseline)

    assert result.returncode == 1
    assert "validity_rate" in result.stdout


def test_lower_is_better_metrics_fail_when_they_rise(tmp_path: Path) -> None:
    baseline = write(tmp_path / "baseline.json", BASELINE)

    assert run_gate(write(tmp_path / "a.json", with_value("fallback_rate", 0.01)), baseline).returncode == 1
    # p95 may move within its tolerance (the remaining budget), but not past it.
    assert run_gate(write(tmp_path / "b.json", with_value("p95_overhead_ms", 9.5)), baseline).returncode == 0
    assert run_gate(write(tmp_path / "c.json", with_value("p95_overhead_ms", 10.5)), baseline).returncode == 1


def test_small_moves_within_tolerance_pass(tmp_path: Path) -> None:
    baseline = write(tmp_path / "baseline.json", BASELINE)

    assert run_gate(write(tmp_path / "a.json", with_value("token_reduction", 0.39)), baseline).returncode == 0
    assert run_gate(write(tmp_path / "b.json", with_value("token_reduction", 0.37)), baseline).returncode == 1


def test_a_missing_metric_fails(tmp_path: Path) -> None:
    baseline = write(tmp_path / "baseline.json", BASELINE)
    partial = {name: m for name, m in BASELINE.items() if name != "validity_rate"}

    result = run_gate(write(tmp_path / "latest.json", partial), baseline)

    assert result.returncode == 1
    assert "validity_rate: missing" in result.stdout


def test_no_baseline_yet_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    latest = write(tmp_path / "latest.json", BASELINE)

    result = run_gate(latest, tmp_path / "baseline.json")

    assert result.returncode == 0
    assert "WARNING" in result.stdout


def test_the_gate_reads_real_tier1_output(tmp_path: Path) -> None:
    # End to end: tier1's own output is a valid baseline, and regressing it fails the gate.
    latest = tmp_path / "latest.json"
    tier1.main(["--out", str(latest), "--sessions", str(FIXTURES / "sessions")])
    baseline = tmp_path / "baseline.json"
    baseline.write_text(latest.read_text())
    assert run_gate(latest, baseline).returncode == 0

    regressed = json.loads(latest.read_text())
    regressed["metrics"]["render_determinism"]["value"] = 0.9
    result = run_gate(write(tmp_path / "regressed.json", regressed["metrics"]), baseline)

    assert result.returncode == 1
    assert "render_determinism" in result.stdout


def test_a_baseline_recorded_over_budget_still_enforces_the_budget(tmp_path: Path) -> None:
    # tier1 sets the p95 tolerance to "budget - value": negative when the baseline itself was
    # over budget. The gate must then fail anything over budget, not accept the slow baseline.
    over = {"p95_overhead_ms": {"value": 11.0, "direction": "lower", "tolerance": 10.0 - 11.0}}
    baseline = write(tmp_path / "baseline.json", over)

    still_slow = write(tmp_path / "a.json", {"p95_overhead_ms": {**over["p95_overhead_ms"], "value": 10.9}})
    back_in_budget = write(tmp_path / "b.json", {"p95_overhead_ms": {**over["p95_overhead_ms"], "value": 9.9}})

    assert run_gate(still_slow, baseline).returncode == 1
    assert run_gate(back_in_budget, baseline).returncode == 0
