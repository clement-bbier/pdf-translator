"""Tests for evaluation/translation_suite.py and its wiring in evaluation/run.py.

No network, no real LLM; every report/baseline path lives in a temp directory.
Run: python tests/test_translation_suite.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm.base import LLMRequest, LLMResponse  # noqa: E402
from app.llm.fake_provider import FakeProvider  # noqa: E402
from evaluation import run, translation_suite  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


class PricedStub:
    """Returns every segment unchanged under a priced model id, 1000/500 tokens."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        text = request.messages[-1]["content"]
        text = text if isinstance(text, str) else text[0]["text"]
        payload = json.loads(text[text.rindex("{") :])
        return LLMResponse(
            text=json.dumps(payload, ensure_ascii=False),
            model="claude-haiku-4-5-20251001",
            input_tokens=1000,
            output_tokens=500,
        )


def _run_main(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = run.main(argv)
    return code, buffer.getvalue()


class Patched:
    """Redirect run.py output paths into a temp directory."""

    def __init__(self, tmp: str) -> None:
        self.tmp = Path(tmp)

    def __enter__(self) -> Patched:
        self.saved = (run.REPORTS_DIR, run.BASELINE_PATH, run.PUBLISH_PATH)
        run.REPORTS_DIR = self.tmp / "reports"
        run.BASELINE_PATH = self.tmp / "baseline.json"
        run.PUBLISH_PATH = self.tmp / "docs" / "evaluation-report.md"
        return self

    def __exit__(self, *exc: object) -> None:
        run.REPORTS_DIR, run.BASELINE_PATH, run.PUBLISH_PATH = self.saved


def test_fake_evaluate() -> None:
    start = time.perf_counter()
    first = translation_suite.evaluate("fake", limit=3)
    elapsed = time.perf_counter() - start
    second = translation_suite.evaluate("fake", limit=3)
    check(elapsed < 5.0, f"evaluate took {elapsed:.1f}s")
    for config in translation_suite.CONFIGURATIONS:
        check(f"{config}.check_pass_rate.mean" in first.metrics, f"missing config {config}")
    check(first.dataset_size == 3, "limit not applied")
    stable = {k: v for k, v in first.metrics.items() if "latency" not in k} == {
        k: v for k, v in second.metrics.items() if "latency" not in k
    }
    check(stable, "two fake evaluations differ")
    stds = [v for k, v in first.metrics.items() if k.endswith(".std") and "latency" not in k]
    check(all(v == 0.0 for v in stds), "fake std is not 0")
    check(run.SUITES["translation"] is translation_suite, "suite not registered")


def test_report() -> None:
    result = translation_suite.evaluate("fake", limit=2)
    text = result.render_markdown()
    check(translation_suite.FAKE_WARNING in text, "fake warning missing")
    check("Provider: fake" in text, "provider missing")
    check(FakeProvider.MODEL_ID in text, "model missing")
    check("Cache: disabled" in text, "cache line missing")
    check("Prompt version: v1" in text, "prompt version missing")
    check("± " in text, "mean ± std cells missing")
    check("no-ops" not in text, "integrated RAG must not be reported as a no-op")
    metrics = result.metrics
    check(
        metrics["glossary.glossary_adherence.mean"] > metrics["no_rag.glossary_adherence.mean"],
        "the glossary configuration must inject terms the no_rag one does not",
    )
    check(
        metrics["glossary_tm.input_tokens.mean"] > metrics["glossary.input_tokens.mean"],
        "the translation memory must add examples to the prompt",
    )
    check(all(seg_text not in text for seg_text in ["公司", "董事会"]), "segment text leaked")


def test_anthropic_unavailable() -> None:
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        try:
            translation_suite.evaluate("anthropic", limit=1)
            check(False, "no SuiteUnavailable without key")
        except translation_suite.SuiteUnavailable:
            pass
        with tempfile.TemporaryDirectory() as tmp, Patched(tmp):
            code, _ = _run_main(["--provider", "anthropic"])
            check(code == 2, f"anthropic without key returned {code}")
            check(not (Path(tmp) / "reports").exists(), "report written without key")
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved


def test_malformed_provider() -> None:
    result = translation_suite.evaluate(
        "fake", limit=3, runs=1, provider_factory=lambda: FakeProvider(invalid_json=True)
    )
    check(result.metrics["no_rag.check_pass_rate.mean"] == 0.0, "pass rate should be 0")
    check(result.failed_calls > 0, "failures not counted")


def test_cost() -> None:
    result = translation_suite.evaluate("fake", limit=2, runs=1, provider_factory=PricedStub)
    # 2 segments x (1000 * 1.00 + 500 * 5.00) / 1e6
    expected = 2 * (1000 * 1.0 + 500 * 5.0) / 1e6
    got = result.metrics["no_rag.cost_usd.mean"]
    check(abs(got - expected) < 1e-9, f"cost {got} != {expected}")
    check(result.metrics["no_rag.input_tokens.mean"] == 2000.0, "input tokens")
    fake = translation_suite.evaluate("fake", limit=1, runs=1)
    check(fake.metrics["no_rag.cost_usd.mean"] == 0.0, "fake cost should be 0")
    check(any("no price for fake-model" in n for n in fake.notes), "no price note missing")


def test_compare() -> None:
    current = {
        "layout": {"written_ratio": 1.0},
        "translation": {"no_rag.latency_ms.mean": 5.0, "no_rag.chrf.mean": 50.0},
        "translation@anthropic": {
            "m.glossary.chrf.mean": 48.0,
            "m.glossary.chrf.std": 1.0,
            "m.glossary.cost_usd.mean": 0.5,
        },
    }
    baseline = {
        "layout": {"written_ratio": 1.0},
        "translation": {"no_rag.latency_ms.mean": 1.0, "no_rag.chrf.mean": 50.0},
        "translation@anthropic": {
            "m.glossary.chrf.mean": 49.5,
            "m.glossary.chrf.std": 0.5,
            "m.glossary.cost_usd.mean": 0.5,
        },
    }
    rows = {(r["suite"], r["metric"]): r["status"] for r in run.compare(current, baseline)}
    check(rows[("layout", "written_ratio")] == "unchanged", "layout row changed")
    check(rows[("translation", "no_rag.latency_ms.mean")] == "info", "latency not info")
    check(rows[("translation", "no_rag.chrf.mean")] == "unchanged", "fake chrf")
    # drop of 1.5 is within 2 * max(1.0, 0.5) = 2.0
    check(rows[("translation@anthropic", "m.glossary.chrf.mean")] == "unchanged", "2-std tol")
    check(rows[("translation@anthropic", "m.glossary.chrf.std")] == "info", "std not info")
    current["translation@anthropic"]["m.glossary.chrf.mean"] = 40.0
    rows = {(r["suite"], r["metric"]): r["status"] for r in run.compare(current, baseline)}
    check(rows[("translation@anthropic", "m.glossary.chrf.mean")] == "regression", "regression")
    check(run.verdict_of([{"status": "info"}]) == "pass", "info must not affect verdict")
    check(translation_suite.metric_direction("a.cost_usd.mean") is False, "cost direction")
    check(translation_suite.metric_direction("a.chrf.mean") is True, "chrf direction")


def test_baseline_merge_and_publish() -> None:
    with tempfile.TemporaryDirectory() as tmp, Patched(tmp):
        run.BASELINE_PATH.write_text(json.dumps({"layout": {"x": 1.0}}), encoding="utf-8")
        code, out = _run_main(["--suite", "translation", "--limit", "2", "--update-baseline"])
        check(code == 2 and "--limit" in out, "limit with update-baseline not refused")
        check(not run.BASELINE_PATH.read_text(encoding="utf-8").count("translation"), "written")
        code, _ = _run_main(["--suite", "translation", "--update-baseline"])
        check(code == 0, f"update-baseline returned {code}")
        merged = json.loads(run.BASELINE_PATH.read_text(encoding="utf-8"))
        check(merged.get("layout") == {"x": 1.0}, "layout section lost")
        check("translation" in merged, "translation section missing")
        report = run.REPORTS_DIR / "translation.md"
        check(report.is_file(), "translation.md not written")
        code, _ = _run_main(["--publish-report"])
        check(code == 0 and run.PUBLISH_PATH.is_file(), "publish failed")
        report.unlink()
        run.PUBLISH_PATH.unlink()
        code, _ = _run_main(["--publish-report"])
        check(code == 1, "publish without source should return 1")


def main() -> int:
    tests = [
        test_fake_evaluate,
        test_report,
        test_anthropic_unavailable,
        test_malformed_provider,
        test_cost,
        test_compare,
        test_baseline_merge_and_publish,
    ]
    for fn in tests:
        before = len(FAILURES)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{fn.__name__} raised {exc!r}")
        print(f"[{'OK' if len(FAILURES) == before else 'FAIL'}] {fn.__name__}")
    print(f"{len(tests)} tests, {len(FAILURES)} failure(s)")
    for message in FAILURES:
        print(f"  - {message}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
