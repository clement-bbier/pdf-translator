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

from evaluation import layout_suite, translation_suite

ROOT = Path(__file__).resolve().parent
BASELINE_PATH = ROOT / "baseline.json"
REPORTS_DIR = ROOT / "reports"
PUBLISH_PATH = ROOT.parent / "docs" / "evaluation-report.md"

SUITES: dict[str, ModuleType] = {
    layout_suite.NAME: layout_suite,
    translation_suite.NAME: translation_suite,
}

# Absolute tolerance per suite; deterministic suites tolerate no movement.
TOLERANCE: dict[str, float] = {layout_suite.NAME: 0.0}

REAL_KEY = f"{translation_suite.NAME}@anthropic"


def compare(
    current: dict[str, dict[str, float]], baseline: dict[str, dict[str, float]]
) -> list[dict]:
    """Return one row per metric with its baseline, current value and status.

    A suite key may carry a variant ('translation@anthropic'); the module is
    resolved from the part before '@'. Optional suite hooks metric_direction
    (None = informational, status 'info') and metric_tolerance override
    DIRECTIONS / TOLERANCE.
    """
    rows: list[dict] = []
    for suite_name, metrics in current.items():
        module = SUITES[suite_name.split("@")[0]]
        directions = getattr(module, "DIRECTIONS", {})
        direction_hook = getattr(module, "metric_direction", None)
        tolerance_hook = getattr(module, "metric_tolerance", None)
        reference = baseline.get(suite_name, {})
        for metric, value in metrics.items():
            base = reference.get(metric)
            if tolerance_hook is not None:
                tolerance = tolerance_hook(suite_name, metric, metrics, reference)
            else:
                tolerance = TOLERANCE.get(suite_name, 0.0)
            higher_is_better = (
                direction_hook(metric)
                if direction_hook is not None
                else directions.get(metric, True)
            )
            if higher_is_better is None:
                status = "info"
            elif base is None:
                status = "new"
            else:
                delta = value - base
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
    parser.add_argument(
        "--provider",
        choices=["fake", "anthropic"],
        default="fake",
        help="translation suite provider (anthropic runs only that suite)",
    )
    parser.add_argument("--limit", type=int, default=None, help="first N dataset segments only")
    parser.add_argument(
        "--publish-report",
        action="store_true",
        help="copy reports/translation.md to docs/evaluation-report.md",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)

    if args.publish_report:
        source = REPORTS_DIR / "translation.md"
        if not source.is_file():
            print(f"nothing to publish: {source} is missing (run `make eval` first)")
            return 1
        PUBLISH_PATH.parent.mkdir(parents=True, exist_ok=True)
        PUBLISH_PATH.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"published: {PUBLISH_PATH}")
        return 0

    started = time.monotonic()
    current: dict[str, dict[str, float]] = {}
    translation_report: str | None = None
    if args.provider == "anthropic":
        try:
            run_result = translation_suite.evaluate("anthropic", limit=args.limit)
        except translation_suite.SuiteUnavailable as exc:
            print(f"translation suite unavailable: {exc}")
            return 2
        current[REAL_KEY] = run_result.metrics
        translation_report = run_result.render_markdown()
    else:
        for name in args.suite or sorted(SUITES):
            if name == translation_suite.NAME:
                run_result = translation_suite.evaluate("fake", limit=args.limit)
                current[name] = run_result.metrics
                translation_report = run_result.render_markdown()
            else:
                current[name] = SUITES[name].run()
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if translation_report is not None:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        (REPORTS_DIR / "translation.md").write_text(translation_report, encoding="utf-8")

    baseline = (
        json.loads(BASELINE_PATH.read_text(encoding="utf-8")) if BASELINE_PATH.is_file() else {}
    )

    if args.update_baseline:
        baseline.update(current)
        BASELINE_PATH.write_text(
            json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"baseline updated: {BASELINE_PATH}")
        return 0

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
