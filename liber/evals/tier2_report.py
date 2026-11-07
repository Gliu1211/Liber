"""Tier 2 results: per-condition statistics, the three comparisons, trends (ARCHITECTURE.md §12).

Input is a list of runs as plain dicts (`asdict(tier2.ConditionRun)`), so a history file can be
re-read and re-reported without the runner. For each task and condition, over its repeats:

    success rate, needle rate             share of repeats the judge passed (None verdicts left out)
    input tokens                          uncached input + cache reads + cache writes, per repeat
    output, cache read/write, thinking dropped, cost (input side), binding errors

each as mean, min, max and standard deviation ("spread"). Then the three comparisons §12 names:

    A vs B   what dropping thinking costs              success(A) - success(B)
    C vs B   how much carry-forward notes recover      success(C) - success(B)
    B/C vs D whether Liber beats client compaction     success(B) - success(D), success(C) - success(D)

each also with the input-token change, per task and averaged over tasks. `trends` reads every
earlier history file for success and input tokens per condition over time.
"""

import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

CONDITION_NAMES = {
    "A": "full context",
    "B": "Liber, thinking dropped",
    "C": "Liber + carry_forward_reasoning",
    "D": "client auto-compaction (simulated)",
}
CONDITIONS = tuple(CONDITION_NAMES)
COMPARISONS = (
    ("A vs B", "A", "B", "what dropping thinking costs"),
    ("C vs B", "C", "B", "how much carry-forward recovers"),
    ("B vs D", "B", "D", "Liber (thinking dropped) vs client compaction"),
    ("C vs D", "C", "D", "Liber + carry-forward vs client compaction"),
)


@dataclass(frozen=True)
class Spread:
    mean: float | None
    low: float | None
    high: float | None
    stdev: float | None
    n: int


def spread(values: list[float]) -> Spread:
    if not values:
        return Spread(None, None, None, None, 0)
    deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    return Spread(statistics.fmean(values), min(values), max(values), deviation, len(values))


def total_input(run: dict[str, Any]) -> int:
    usage = run["usage"]
    return int(usage["input_tokens"]) + int(usage["cache_read"]) + int(usage["cache_write"])


@dataclass(frozen=True)
class Stats:
    """One task's runs under one condition, over its repeats."""

    success: Spread  # 1.0 = the judge passed it, 0.0 = it didn't
    needle: Spread
    input_tokens: Spread
    output_tokens: Spread
    cache_read: Spread
    cache_write: Spread
    thinking_dropped: Spread
    cost: Spread
    binding_errors: int
    errors: int


def condition_stats(runs: list[dict[str, Any]]) -> Stats:
    return Stats(
        success=spread([float(r["success"]) for r in runs if r["success"] is not None]),
        needle=spread([float(r["needle_kept"]) for r in runs if r["needle_kept"] is not None]),
        input_tokens=spread([float(total_input(r)) for r in runs]),
        output_tokens=spread([float(r["usage"]["output_tokens"]) for r in runs]),
        cache_read=spread([float(r["usage"]["cache_read"]) for r in runs]),
        cache_write=spread([float(r["usage"]["cache_write"]) for r in runs]),
        thinking_dropped=spread([float(r["usage"]["thinking_dropped"]) for r in runs]),
        cost=spread([float(r["cost"]) for r in runs if r["cost"] is not None]),
        binding_errors=sum(len(r["binding_errors"]) for r in runs),
        errors=sum(len(r["errors"]) for r in runs),
    )


def summarize(runs: list[dict[str, Any]]) -> dict[str, dict[str, Stats]]:
    """task -> condition -> stats."""
    tasks = sorted({r["task"] for r in runs})
    return {
        task: {
            condition: condition_stats([r for r in runs if r["task"] == task and r["condition"] == condition])
            for condition in CONDITIONS
        }
        for task in tasks
    }


@dataclass(frozen=True)
class Comparison:
    name: str
    meaning: str
    success_delta: float | None  # first minus second, in success rate (-1..1)
    input_change: float | None  # first's input tokens relative to second's (0.2 = 20% more)


def compare(stats: dict[str, Stats]) -> list[Comparison]:
    """The §12 comparisons for one task's stats."""
    result = []
    for name, first, second, meaning in COMPARISONS:
        a, b = stats[first], stats[second]
        delta = None
        if a.success.mean is not None and b.success.mean is not None:
            delta = a.success.mean - b.success.mean
        change = None
        if a.input_tokens.mean is not None and b.input_tokens.mean:
            change = a.input_tokens.mean / b.input_tokens.mean - 1
        result.append(Comparison(name, meaning, delta, change))
    return result


def overall(per_task: dict[str, list[Comparison]]) -> list[Comparison]:
    """Each comparison averaged over the tasks that have a value for it."""
    result = []
    for index, (name, _, _, meaning) in enumerate(COMPARISONS):
        deltas = [d for c in per_task.values() if (d := c[index].success_delta) is not None]
        changes = [d for c in per_task.values() if (d := c[index].input_change) is not None]
        result.append(
            Comparison(
                name,
                meaning,
                statistics.fmean(deltas) if deltas else None,
                statistics.fmean(changes) if changes else None,
            )
        )
    return result


def trends(history: Path) -> list[tuple[str, dict[str, tuple[float | None, float | None]]]]:
    """(date, condition -> (mean success, mean input tokens)) for every history file, oldest first."""
    points = []
    for path in history.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            runs = data["runs"]
        except (ValueError, KeyError, OSError):
            continue  # not a Tier 2 result
        by_condition = {}
        for condition in CONDITIONS:
            chosen = [r for r in runs if r["condition"] == condition]
            verdicts = [float(r["success"]) for r in chosen if r["success"] is not None]
            tokens = [float(total_input(r)) for r in chosen]
            by_condition[condition] = (
                statistics.fmean(verdicts) if verdicts else None,
                statistics.fmean(tokens) if tokens else None,
            )
        points.append((str(data.get("date", path.stem)), by_condition))
    return sorted(points, key=lambda point: point[0])  # "2026-11-07" before "2026-11-07-110000"


NONE = "n/a"  # no value (no verdict, no price)


# --- Markdown -----------------------------------------------------------------------------------------


def pct(value: float | None) -> str:
    return NONE if value is None else f"{value:.0%}"


def signed_pct(value: float | None) -> str:
    return NONE if value is None else f"{value:+.0%}"


def number(value: float | None) -> str:
    return NONE if value is None else f"{value:,.0f}"


def dollars(value: float | None) -> str:
    return NONE if value is None else f"${value:.4f}"


def with_spread(s: Spread, show: Callable[[float | None], str] = number) -> str:
    if s.mean is None:
        return NONE
    if s.n < 2:
        return show(s.mean)
    return f"{show(s.mean)} ({show(s.low)} to {show(s.high)}, sd {show(s.stdev)})"


def report(runs: list[dict[str, Any]], meta: dict[str, Any], history: Path | None = None) -> str:
    stats = summarize(runs)
    per_task = {task: compare(s) for task, s in stats.items()}
    lines = [
        f"# Tier 2 — {meta.get('date', '')}",
        "",
        f"Model `{meta.get('model')}`, judge `{meta.get('judge_model')}`, summaries `{meta.get('summary_model')}`, "
        f"{meta.get('repeats')} repeat(s) per condition. Spread: mean (min to max, sd).",
        "",
        "## Comparisons (averaged over tasks)",
        "",
        "| Comparison | Meaning | Success difference | Input tokens |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {c.name} | {c.meaning} | {signed_pct(c.success_delta)} | {signed_pct(c.input_change)} |"
        for c in overall(per_task)
    ]
    header = (
        "| Condition | Success | Needle kept | Input tokens | Output tokens | Cache reads | Cache writes "
        "| Thinking dropped | Cost (input) | Binding errors | Other errors |"
    )
    for task, by_condition in stats.items():
        lines += ["", f"## {task}", "", header, "|---" * 11 + "|"]
        for condition, s in by_condition.items():
            cells = [
                with_spread(s.success, pct),
                with_spread(s.needle, pct),
                with_spread(s.input_tokens),
                with_spread(s.output_tokens),
                with_spread(s.cache_read),
                with_spread(s.cache_write),
                with_spread(s.thinking_dropped),
                with_spread(s.cost, dollars),
                str(s.binding_errors),
                str(s.errors),
            ]
            lines.append(f"| {condition}: {CONDITION_NAMES[condition]} | " + " | ".join(cells) + " |")
        lines += ["", "| Comparison | Success difference | Input tokens |", "|---|---|---|"]
        lines += [
            f"| {c.name} | {signed_pct(c.success_delta)} | {signed_pct(c.input_change)} |" for c in per_task[task]
        ]
    failures = [(r["task"], r["condition"], r["repeat"], e) for r in runs for e in r["binding_errors"]]
    if failures:
        lines += ["", "## Binding errors (by failing path)", ""]
        lines += [f"- {task} {condition} #{repeat}: `{error}`" for task, condition, repeat, error in failures]
    if history is not None:
        points = trends(history)
        if points:
            lines += ["", "## Trends", "", "| Date | " + " | ".join(f"{c} success · input" for c in CONDITIONS) + " |"]
            lines.append("|---" * (len(CONDITIONS) + 1) + "|")
            for date, point in points:
                cells = [f"{pct(point[c][0])} · {number(point[c][1])}" for c in CONDITIONS]
                lines.append(f"| {date} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Notes: cost is input side only (the pricing table has no output prices), and B/C's worker",
        "summary calls aren't included; D's compaction call is.",
        "",
    ]
    return "\n".join(lines)


def write_history(
    directory: Path, runs: list[dict[str, Any]], meta: dict[str, Any], now: datetime
) -> tuple[Path, Path]:
    """evals/history/<date>.json and .md (a second run that day gets the time added)."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = now.strftime("%Y-%m-%d")
    if (directory / f"{stem}.json").exists():
        stem = now.strftime("%Y-%m-%d-%H%M%S")
    meta = {**meta, "date": stem}
    json_path, report_path = directory / f"{stem}.json", directory / f"{stem}.md"
    json_path.write_text(json.dumps({**meta, "runs": runs}, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(report(runs, meta, directory), encoding="utf-8")
    return json_path, report_path
