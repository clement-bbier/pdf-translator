"""Run the evaluation suites and compare them with the committed baseline.

    python -m evaluation.run                    # run, compare, write reports
    python -m evaluation.run --update-baseline  # human only: accept current numbers

Outputs (gitignored runtime state):
    evaluation/reports/latest.json   machine-readable result + verdict
    evaluation/reports/latest.md     human-readable comparison table

Exit code: 0 when no metric regressed, 1 on any regression. A metric
regresses when it moves in its bad direction by more than the tolerance (0 for
deterministic suites). evaluation/baseline.json is protected by the
guard_write/guard_bash hooks: agents cannot edit it to hide a regression.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from evaluation import layout_suite

ROOT = Path(__file__).resolve().parent
BASELINE_PATH = ROOT / "baseline.json"
REPORTS_DIR = ROOT / "reports"

SUITES: dict[str, ModuleType] = {layout_suite.NAME: layout_suite}

# Absolute tolerance per suite; deterministic suites tolerate no movement.
TOLERANCE: dict[str, float] = {layout_suite.NAME: 0.0}


def compare(
    current: dict[str, dict[str, float]], baseline: dict[str, dict[str, float]]
) -> list[dict]:
    """Return one row per metric with its baseline, current value and status."""
    rows: list[dict] = []
    for suite_name, metrics in current.items():
        directions = SUITES[suite_name].DIRECTIONS
        tolerance = TOLERANCE.get(suite_name, 0.0)
        reference = baseline.get(suite_name, {})
        for metric, value in metrics.items():
            base = reference.get(metric)
            if base is None:
                status = "new"
            else:
                delta = value - base
                higher_is_better = directions.get(metric, True)
                worse = -delta if higher_is_better else delta
                if worse > tolerance:
                    status = "regression"
                elif worse < -tolerance:
                    status = "improvement"
                else:
                    status = "unchanged"
            rows.append(
                {
                    "suite": suite_name,
                    "metric": metric,
                    "baseline": base,
                    "current": value,
                    "status": status,
                }
            )
    return rows


def verdict_of(rows: list[dict]) -> str:
    statuses = {row["status"] for row in rows}
    if "regression" in statuses:
        return "regression"
    if "improvement" in statuses:
        return "improvement"
    return "pass"


def render_markdown(rows: list[dict], verdict: str, generated_at: str) -> str:
    lines = [
        "# Evaluation report",
        "",
        f"Generated: {generated_at}  ",
        f"Verdict: **{verdict}**",
        "",
        "| Suite | Metric | Baseline | Current | Status |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for row in rows:
        base = "-" if row["baseline"] is None else f"{row['baseline']:g}"
        lines.append(
            f"| {row['suite']} | {row['metric']} | {base} | {row['current']:g} | {row['status']} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation suites.")
    parser.add_argument("--suite", choices=sorted(SUITES), action="append", help="suites to run")
    parser.add_argument("--update-baseline", action="store_true", help="overwrite baseline.json")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)

    started = time.monotonic()
    current = {name: SUITES[name].run() for name in (args.suite or sorted(SUITES))}
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.update_baseline:
        BASELINE_PATH.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"baseline updated: {BASELINE_PATH}")
        return 0

    baseline = (
        json.loads(BASELINE_PATH.read_text(encoding="utf-8")) if BASELINE_PATH.is_file() else {}
    )
    rows = compare(current, baseline)
    verdict = verdict_of(rows)
    regressions = [row for row in rows if row["status"] == "regression"]
    headline = (
        f"{len(regressions)} regression(s): "
        + ", ".join(f"{r['suite']}.{r['metric']}" for r in regressions)
        if regressions
        else f"{len(rows)} metric(s), no regression"
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": generated_at,
        "verdict": verdict,
        "headline": headline,
        "duration_s": round(time.monotonic() - started, 2),
        "metrics": current,
        "comparison": rows,
    }
    (REPORTS_DIR / "latest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (REPORTS_DIR / "latest.md").write_text(
        render_markdown(rows, verdict, generated_at), encoding="utf-8"
    )

    print(render_markdown(rows, verdict, generated_at))
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
