"""Tier 2 results: per-condition statistics, the three comparisons, trends (ARCHITECTURE.md §12).

Input is a list of runs as plain dicts (`asdict(tier2.ConditionRun)`), so a history file can be
re-read and re-reported without the runner. For each task and condition, over its repeats:

    success rate, needle rate   share of judged answers that passed. A binding break counts as a
                                fail (tier2.grade); a missing verdict (an API or judge failure) is
                                left out, and every rate says how many verdicts it rests on.
    input tokens                all-in: uncached input + cache reads + cache writes, including D's
                                compaction call, averaged over complete runs (no errors)
    output, cache read/write, thinking dropped, cost (input side), binding and other errors

each as mean, min, max and standard deviation ("spread"). Then the three comparisons §12 names:

    A vs B   what dropping thinking costs              success(A) - success(B)
    C vs B   how much carry-forward notes recover      success(C) - success(B)
    B/C vs D whether Liber beats client compaction     success(B) - success(D), success(C) - success(D)

each with the input-token change, per task, and averaged separately over reasoning-dependent tasks
and the others (a factual task would otherwise water down what dropping thinking costs).
`trends` reads every history file for success and input tokens per task and condition over time.
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
    "E": "Liber, chosen items kept",  # docs/decisions/0016; only tasks with keep_tool_results
}
CONDITIONS = tuple(CONDITION_NAMES)
COMPARISONS = (
    ("A vs B", "A", "B", "what dropping thinking costs"),
    ("C vs B", "C", "B", "how much carry-forward recovers"),
    ("B vs D", "B", "D", "Liber (thinking dropped) vs client compaction"),
    ("C vs D", "C", "D", "Liber + carry-forward vs client compaction"),
    ("A vs E", "A", "E", "what keeping chosen items leaves unrecovered"),
    ("E vs B", "E", "B", "the value of choosing what to keep"),
    ("E vs D", "E", "D", "what client compaction can't do"),
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
    """All input tokens a condition used: its requests plus its own overhead (D's compaction call)."""
    total = 0
    for usage in (run["usage"], run["overhead"]):
        total += int(usage["input_tokens"]) + int(usage["cache_read"]) + int(usage["cache_write"])
    return total


def complete(run: dict[str, Any]) -> bool:
    """Every request of the run went through: its token counts cover the whole condition."""
    return not run["errors"] and not run["binding_errors"]


@dataclass(frozen=True)
class Stats:
    """One task's runs under one condition, over its repeats."""

    runs: int
    success: Spread  # 1.0 = the judge passed it, 0.0 = it didn't
    needle: Spread
    input_tokens: Spread  # complete runs only
    output_tokens: Spread
    cache_read: Spread
    cache_write: Spread
    thinking_dropped: Spread
    cost: Spread
    binding_errors: int
    errors: int
    summary_had_evidence: int  # B/C runs whose summary kept a task's evidence marker
    evidence_checked: int


def condition_stats(runs: list[dict[str, Any]]) -> Stats:
    whole = [r for r in runs if complete(r)]
    checked = [r for r in runs if r.get("summary_has_evidence") is not None]
    return Stats(
        runs=len(runs),
        success=spread([float(r["success"]) for r in runs if r["success"] is not None]),
        needle=spread([float(r["needle_kept"]) for r in runs if r["needle_kept"] is not None]),
        input_tokens=spread([float(total_input(r)) for r in whole]),
        output_tokens=spread([float(r["usage"]["output_tokens"] + r["overhead"]["output_tokens"]) for r in whole]),
        cache_read=spread([float(r["usage"]["cache_read"]) for r in whole]),
        cache_write=spread([float(r["usage"]["cache_write"]) for r in whole]),
        thinking_dropped=spread([float(r["usage"]["thinking_dropped"]) for r in whole]),
        cost=spread([float(r["cost"]) for r in whole if r["cost"] is not None]),
        binding_errors=sum(len(r["binding_errors"]) for r in runs),
        errors=sum(len(r["errors"]) for r in runs),
        summary_had_evidence=sum(1 for r in checked if r["summary_has_evidence"]),
        evidence_checked=len(checked),
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
    success_delta: float | None  # first minus second, in success rate (-1..1): + means the first did better
    input_change: float | None  # first's input tokens relative to second's: + means the first used more


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


def reasoning_tasks(runs: list[dict[str, Any]]) -> set[str]:
    return {r["task"] for r in runs if r.get("reasoning_dependent")}


Trend = tuple[str, str, dict[str, tuple[float | None, float | None]]]  # date, task, condition -> values


def trends(history: Path) -> list[Trend]:
    """(date, task, condition -> (mean success, mean input tokens)) for every history file.

    Per task, so adding a task or changing repeats doesn't move an existing task's line. Sorted by
    task, then date ("2026-11-07" before "2026-11-07-110000").
    """
    points = []
    for path in history.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            runs = data["runs"]
        except (ValueError, KeyError, OSError):
            continue  # not a Tier 2 result
        for task, by_condition in summarize(runs).items():
            values = {c: (s.success.mean, s.input_tokens.mean) for c, s in by_condition.items()}
            points.append((str(data.get("date", path.stem)), task, values))
    return sorted(points, key=lambda point: (point[1], point[0]))


# --- Markdown -----------------------------------------------------------------------------------------

NONE = "n/a"  # no value (no verdict, no price)


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


def rate(s: Spread, runs: int) -> str:
    """A pass rate and how many verdicts it rests on, e.g. "67% (0% to 100%, sd 58%) [3/3]"."""
    return f"{with_spread(s, pct)} [{s.n}/{runs}]"


def comparison_table(comparisons: list[Comparison], with_meaning: bool) -> list[str]:
    if with_meaning:
        lines = ["| Comparison | Meaning | Success difference | Input tokens |", "|---|---|---|---|"]
        return lines + [
            f"| {c.name} | {c.meaning} | {signed_pct(c.success_delta)} | {signed_pct(c.input_change)} |"
            for c in comparisons
        ]
    lines = ["| Comparison | Success difference | Input tokens |", "|---|---|---|"]
    return lines + [f"| {c.name} | {signed_pct(c.success_delta)} | {signed_pct(c.input_change)} |" for c in comparisons]


def report(runs: list[dict[str, Any]], meta: dict[str, Any], history: Path | None = None) -> str:
    stats = summarize(runs)
    per_task = {task: compare(s) for task, s in stats.items()}
    reasoning = reasoning_tasks(runs)
    lines = [
        f"# Tier 2 — {meta.get('date', '')}",
        "",
        f"Model `{meta.get('model')}`, judge `{meta.get('judge_model')}`, summaries `{meta.get('summary_model')}`, "
        f"{meta.get('repeats')} repeat(s) per condition. Spread: mean (min to max, sd); [k/n]: verdicts out of runs.",
        "Success difference: + means the first condition did better. Input tokens: + means the first used more.",
    ]
    groups = [
        ("reasoning-dependent tasks", {t: c for t, c in per_task.items() if t in reasoning}),
        ("other tasks", {t: c for t, c in per_task.items() if t not in reasoning}),
    ]
    for label, chosen in groups:
        if chosen:
            lines += ["", f"## Comparisons, averaged over {label} ({', '.join(sorted(chosen))})", ""]
            lines += comparison_table(overall(chosen), with_meaning=True)
    header = (
        "| Condition | Success | Needle kept | Input tokens | Output tokens | Cache reads | Cache writes "
        "| Thinking dropped | Cost (input) | Binding errors | Other errors | Summary had evidence |"
    )
    for task, by_condition in stats.items():
        kind = "reasoning-dependent" if task in reasoning else "factual"
        lines += ["", f"## {task} ({kind})", "", header, "|---" * 12 + "|"]
        for condition, s in by_condition.items():
            evidence = f"{s.summary_had_evidence}/{s.evidence_checked}" if s.evidence_checked else NONE
            cells = [
                rate(s.success, s.runs),
                rate(s.needle, s.runs),
                with_spread(s.input_tokens),
                with_spread(s.output_tokens),
                with_spread(s.cache_read),
                with_spread(s.cache_write),
                with_spread(s.thinking_dropped),
                with_spread(s.cost, dollars),
                str(s.binding_errors),
                str(s.errors),
                evidence,
            ]
            lines.append(f"| {condition}: {CONDITION_NAMES[condition]} | " + " | ".join(cells) + " |")
        lines += ["", *comparison_table(per_task[task], with_meaning=False)]
        if by_condition["B"].summary_had_evidence:
            lines += [
                "",
                "B's summary kept the evidence on some repeats: there, B's result measures the summary, "
                "not what dropping thinking costs.",
            ]
    failures = [(r["task"], r["condition"], r["repeat"], e) for r in runs for e in r["binding_errors"]]
    if failures:
        lines += ["", "## Binding errors (by failing path)", ""]
        lines += [f"- {task} {condition} #{repeat}: `{error}`" for task, condition, repeat, error in failures]
    problems = [(r["task"], r["condition"], r["repeat"], e) for r in runs for e in r["errors"]]
    if problems:
        lines += ["", "## Other errors", ""]
        lines += [f"- {task} {condition} #{repeat}: {error}" for task, condition, repeat, error in problems]
    # A model that tried a tool during the questions got nothing back (tier2.turn), but say so.
    refused = [(r["task"], r["condition"], r["repeat"], r.get("refused_tool_calls") or []) for r in runs]
    refused = [(task, condition, repeat, calls) for task, condition, repeat, calls in refused if calls]
    if refused:
        lines += ["", "## Tool calls refused during the questions", ""]
        lines += [f"- {task} {condition} #{repeat}: {', '.join(calls)}" for task, condition, repeat, calls in refused]
    if history is not None:
        points = trends(history)
        if points:
            columns = " | ".join(f"{c} success · input" for c in CONDITIONS)
            lines += ["", "## Trends", "", f"| Task | Date | {columns} |", "|---" * (len(CONDITIONS) + 2) + "|"]
            for date, task, values in points:
                cells = [f"{pct(values[c][0])} · {number(values[c][1])}" for c in CONDITIONS]
                lines.append(f"| {task} | {date} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Notes: cost is input side only (the pricing table has no output prices); B/C's worker summary calls",
        "aren't metered, D's compaction call is. The tasks set no prompt-cache markers, so cache reads and",
        "writes are only what the API caches on its own. All conditions share each repeat's history, so",
        "differences within a repeat are paired; the spread shown is per condition.",
        "",
    ]
    return "\n".join(lines)


def history_paths(directory: Path, now: datetime) -> tuple[Path, Path]:
    """evals/history/<date>.json and .md for a new run (a second run that day gets the time added)."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = now.strftime("%Y-%m-%d")
    if (directory / f"{stem}.json").exists():
        stem = now.strftime("%Y-%m-%d-%H%M%S")
    return directory / f"{stem}.json", directory / f"{stem}.md"


def save_history(json_path: Path, report_path: Path, runs: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    """Write (or rewrite, as the run goes on) the results and their report."""
    meta = {**meta, "date": json_path.stem}
    json_path.write_text(json.dumps({**meta, "runs": runs}, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(report(runs, meta, json_path.parent), encoding="utf-8")


def write_history(
    directory: Path, runs: list[dict[str, Any]], meta: dict[str, Any], now: datetime
) -> tuple[Path, Path]:
    json_path, report_path = history_paths(directory, now)
    save_history(json_path, report_path, runs, meta)
    return json_path, report_path
