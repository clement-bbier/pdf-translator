"""Integration tests for the pipeline: run_job end to end with MockTranslator.

No network access. Run: python tests/test_pipeline.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from app import config as app_config  # noqa: E402
from app.pipeline import OUTPUT_DIR, run_job  # noqa: E402
from app.translator import MockTranslator, TranslationError  # noqa: E402
from tests import make_fixtures  # noqa: E402

FAILURES: list[str] = []

_JOB_DIRS_TO_CLEAN: list[Path] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def _mock_config() -> dict:
    config = dict(app_config.load_config())
    config["provider"] = "mock"
    return config


def _run_and_track(files: list[Path], src: str, tgt: str, config: dict):
    """Run a job and remember its output dir for cleanup."""
    result = run_job(files, src, tgt, config)
    _JOB_DIRS_TO_CLEAN.append(OUTPUT_DIR / result.job_id)
    return result


# --------------------------------------------------------------------------- #
# 1. run_job basic behaviour with MockTranslator
# --------------------------------------------------------------------------- #


def test_run_job_mock_simple() -> None:
    """run_job on simple.pdf produces stats and an output file."""
    fixture = make_fixtures.FIXTURES_DIR / "simple.pdf"
    result = _run_and_track([fixture], "zh", "fr", _mock_config())

    check(len(result.files) == 1, "expected exactly one FileResult")
    file_result = result.files[0]

    check(file_result.stats.total_blocks > 0, "total_blocks should be > 0")
    check(file_result.stats.api_calls > 0, "api_calls should be > 0 on a fresh cache")
    check(file_result.output_path is not None, "output_path should be set")
    check(
        Path(file_result.output_path).is_file(),
        f"output file missing: {file_result.output_path}",
    )
    check(
        Path(file_result.output_path).parent == OUTPUT_DIR / result.job_id,
        "output file should live under output/<job_id>/",
    )


# --------------------------------------------------------------------------- #
# 2. dedup persists across runs (cache file, not in-memory only)
# --------------------------------------------------------------------------- #


def test_dedup_persists_across_runs() -> None:
    """A second run_job on the same file hits the cache for every unique block."""
    fixture = make_fixtures.FIXTURES_DIR / "simple.pdf"
    config = _mock_config()

    first = _run_and_track([fixture], "zh", "fr", config)
    first_stats = first.files[0].stats

    second = _run_and_track([fixture], "zh", "fr", config)
    second_stats = second.files[0].stats

    check(
        second_stats.cache_hits == first_stats.unique_blocks,
        f"expected cache_hits == {first_stats.unique_blocks}, got {second_stats.cache_hits}",
    )
    check(second_stats.api_calls == 0, "second run should not call the translator at all")


# --------------------------------------------------------------------------- #
# 3. invalid files are rejected before any processing
# --------------------------------------------------------------------------- #


def test_invalid_file_rejected() -> None:
    """A too-large file and a file with the wrong magic bytes both raise ValueError."""
    config = _mock_config()
    before = {entry.name for entry in OUTPUT_DIR.iterdir() if entry.is_dir()}

    with tempfile.TemporaryDirectory() as tmp:
        too_big = Path(tmp) / "too_big.pdf"
        max_mb = float(config.get("max_file_mb", 20))
        with too_big.open("wb") as handle:
            handle.write(b"%PDF-1.4\n")
            handle.seek(int(max_mb * 1024 * 1024) + 1024)
            handle.write(b"\0")

        raised = False
        try:
            run_job([too_big], "zh", "fr", config)
        except ValueError:
            raised = True
        check(raised, "oversized file should raise ValueError")

        bad_magic = Path(tmp) / "bad_magic.pdf"
        bad_magic.write_bytes(b"this is not a pdf file at all")

        raised = False
        try:
            run_job([bad_magic], "zh", "fr", config)
        except ValueError:
            raised = True
        check(raised, "file with wrong magic bytes should raise ValueError")

    after = {entry.name for entry in OUTPUT_DIR.iterdir() if entry.is_dir()}
    check(after == before, f"invalid files should not create output dirs: {after - before}")


# --------------------------------------------------------------------------- #
# 4. scanned pages are excluded from translation and flagged
# --------------------------------------------------------------------------- #


def test_scanned_page_handling() -> None:
    """Scanned pages are skipped, counted, get no API calls, and gain a cover page."""
    fixture = make_fixtures.FIXTURES_DIR / "scanned.pdf"
    source = pymupdf.open(fixture)
    source_pages = source.page_count
    source.close()

    result = _run_and_track([fixture], "zh", "fr", _mock_config())
    file_result = result.files[0]

    check(file_result.scanned_page_count > 0, "scanned_page_count should be > 0")
    check(file_result.stats.api_calls == 0, "an all-scanned file should trigger no API calls")

    out_doc = pymupdf.open(file_result.output_path)
    try:
        check(
            out_doc.page_count == source_pages + 1,
            f"expected a cover page: {source_pages + 1} pages, got {out_doc.page_count}",
        )
        cover_text = out_doc[0].get_text()
        check("WARNING" in cover_text, "cover page should warn about scanned pages")
    finally:
        out_doc.close()


# --------------------------------------------------------------------------- #
# 5. a failing batch does not kill the job
# --------------------------------------------------------------------------- #


class _FlakyTranslator:
    """Raises on the 2nd call, translates normally otherwise."""

    def __init__(self) -> None:
        self.calls = 0

    def translate_blocks(self, blocks: list[str], src: str, tgt: str) -> list[str]:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("simulated gateway failure")
        return MockTranslator().translate_blocks(blocks, src, tgt)


def test_partial_batch_failure_resilience() -> None:
    """A batch that raises is recorded as a warning; the job still finishes."""
    import app.pipeline as pipeline_module

    fixture = make_fixtures.FIXTURES_DIR / "simple.pdf"
    config = _mock_config()
    config["batch_size"] = 1  # force multiple batches so a mid-job failure is exercised

    flaky = _FlakyTranslator()
    original_get_translator = pipeline_module.get_translator
    pipeline_module.get_translator = lambda cfg: flaky  # type: ignore[assignment]

    try:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            original_cache_cls = pipeline_module.TranslationCache
            pipeline_module.TranslationCache = lambda: original_cache_cls(cache_path)  # type: ignore[assignment]
            try:
                result = _run_and_track([fixture], "zh", "fr", config)
            finally:
                pipeline_module.TranslationCache = original_cache_cls  # type: ignore[assignment]
    finally:
        pipeline_module.get_translator = original_get_translator  # type: ignore[assignment]

    check(len(result.warnings) >= 1, "a failed batch should produce at least one warning")
    check(flaky.calls > 2, "translation should continue after the failing batch")

    file_result = result.files[0]
    check(file_result.output_path is not None, "job should still produce an output file")
    check(Path(file_result.output_path).is_file(), "output file should exist despite the failure")


# --------------------------------------------------------------------------- #
# 6. a block the rebuilder cannot fit is reported, never dropped silently
# --------------------------------------------------------------------------- #


class _OversizedTranslator:
    """Returns translations far too long for dense_layout.pdf's tight boxes."""

    def translate_blocks(self, blocks: list[str], src: str, tgt: str) -> list[str]:
        filler = (
            "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
            "tempor incididunt ut labore et dolore magna aliqua "
        )
        return [(text + " " + filler * 10) for text in blocks]


def test_unfittable_block_reported_in_job_warnings() -> None:
    """rebuild()'s blocks_failed must surface as a JobResult/FileResult warning."""
    import app.pipeline as pipeline_module

    fixture = make_fixtures.FIXTURES_DIR / "dense_layout.pdf"
    config = _mock_config()

    oversized = _OversizedTranslator()
    original_get_translator = pipeline_module.get_translator
    pipeline_module.get_translator = lambda cfg: oversized  # type: ignore[assignment]

    try:
        result = _run_and_track([fixture], "en", "fr", config)
    finally:
        pipeline_module.get_translator = original_get_translator  # type: ignore[assignment]

    file_result = result.files[0]
    check(
        any("could not fit" in warning for warning in file_result.warnings),
        f"expected a 'could not fit' warning on the file result, got {file_result.warnings}",
    )
    check(
        any("could not fit" in warning for warning in result.warnings),
        "the file-level warning should also be aggregated onto JobResult.warnings",
    )
    check(file_result.output_path is not None, "job should still produce an output file")
    check(Path(file_result.output_path).is_file(), "output file should exist despite the failure")


# --------------------------------------------------------------------------- #
# 7. multimodal path for scanned pages
# --------------------------------------------------------------------------- #


def _run_scanned(config: dict, translator=None):
    """Run a job on scanned.pdf, optionally forcing a specific translator."""
    import app.pipeline as pipeline_module

    fixture = make_fixtures.FIXTURES_DIR / "scanned.pdf"
    original_get_translator = pipeline_module.get_translator
    if translator is not None:
        pipeline_module.get_translator = lambda cfg: translator  # type: ignore[assignment]
    try:
        return _run_and_track([fixture], "zh", "fr", config)
    finally:
        pipeline_module.get_translator = original_get_translator  # type: ignore[assignment]


def _output_text(file_result) -> str:
    """Concatenate the text of every page of a job's output PDF."""
    doc = pymupdf.open(file_result.output_path)
    try:
        return "".join(doc[index].get_text() for index in range(doc.page_count))
    finally:
        doc.close()


def test_multimodal_disabled_is_unchanged() -> None:
    """multimodal_enabled=false keeps the previous behaviour exactly."""
    config = _mock_config()
    config["multimodal_enabled"] = False

    result = _run_scanned(config)
    file_result = result.files[0]
    text = _output_text(file_result)

    check(file_result.stats.api_calls == 0, "the disabled path must not call the model")
    check("WARNING" in text, "the warning cover page should still be inserted")
    check(
        any("not translated" in warning for warning in result.warnings),
        f"expected the untranslated-scan warning, got {result.warnings}",
    )


def test_multimodal_enabled_translates_scanned_page() -> None:
    """multimodal_enabled=true really translates the scan: no cover page left."""
    config = _mock_config()
    config["multimodal_enabled"] = True

    source = pymupdf.open(make_fixtures.FIXTURES_DIR / "scanned.pdf")
    source_pages = source.page_count
    source.close()

    result = _run_scanned(config)
    file_result = result.files[0]
    text = _output_text(file_result)

    doc = pymupdf.open(file_result.output_path)
    page_count = doc.page_count
    doc.close()

    check(
        "Scanned page translated" in text,
        "the mock page translation should appear in the output",
    )
    check("WARNING" not in text, "a translated scan must not get a warning cover page")
    check(
        page_count == source_pages,
        f"page count should stay {source_pages}, got {page_count}",
    )
    check(file_result.stats.api_calls >= 1, "the multimodal call should be counted")
    check(
        not any("not translated" in warning for warning in result.warnings),
        f"no untranslated warning expected, got {result.warnings}",
    )


def test_multimodal_failure_falls_back_with_warning() -> None:
    """A failing multimodal call degrades to the cover page, and the job survives."""

    class _FailingImageTranslator(MockTranslator):
        """Translates text normally, always fails on page images."""

        def translate_page_image(self, image_bytes: bytes, src: str, tgt: str) -> str:
            raise TranslationError("simulated multimodal failure")

    config = _mock_config()
    config["multimodal_enabled"] = True

    result = _run_scanned(config, translator=_FailingImageTranslator())
    file_result = result.files[0]
    text = _output_text(file_result)

    check(file_result.output_path is not None, "the job should still produce an output")
    check(
        Path(file_result.output_path).is_file(),
        "the output file should exist despite the failure",
    )
    check("WARNING" in text, "a failed scan should fall back to the cover page")
    check(
        any("could not be translated" in warning for warning in result.warnings),
        f"expected an explicit failure warning, got {result.warnings}",
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def _cleanup() -> None:
    """Remove job output directories and the shared cache created by these tests."""
    for job_dir in _JOB_DIRS_TO_CLEAN:
        shutil.rmtree(job_dir, ignore_errors=True)
    default_cache = ROOT / "tmp" / "cache.json"
    if default_cache.is_file():
        default_cache.unlink()


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()
    make_fixtures.main([])

    tests = [
        test_run_job_mock_simple,
        test_dedup_persists_across_runs,
        test_invalid_file_rejected,
        test_scanned_page_handling,
        test_partial_batch_failure_resilience,
        test_unfittable_block_reported_in_job_warnings,
        test_multimodal_disabled_is_unchanged,
        test_multimodal_enabled_translates_scanned_page,
        test_multimodal_failure_falls_back_with_warning,
    ]

    failures_before = 0
    for test in tests:
        failures_before = len(FAILURES)
        try:
            test()
        except Exception as error:  # noqa: BLE001 - report and continue
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
        status = "OK" if len(FAILURES) == failures_before else "FAIL"
        print(f"[{status}] {test.__name__}")

    _cleanup()

    print(f"\n{len(tests)} tests, {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
