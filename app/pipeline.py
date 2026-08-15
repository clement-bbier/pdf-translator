"""Translation job orchestration: extraction, translation, PDF reconstruction."""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pymupdf

from app import config as app_config
from app.cache import TranslationCache
from app.extractor import extract
from app.models import Block, DocumentData
from app.rebuilder import rebuild
from app.scanned import rebuild_scanned_page, render_page_image
from app.translator import JobStats, Translator, get_translator

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

ProgressCallback = Callable[[str], None]

PDF_MAGIC = b"%PDF-"


def _validate_file(file_path: Path, config: dict) -> None:
    """Reject a file before any processing: wrong magic bytes or too large."""
    max_file_mb = float(config.get("max_file_mb", 20))
    size_mb = file_path.stat().st_size / (1024 * 1024)
    if size_mb > max_file_mb:
        raise ValueError(
            f"{file_path.name}: file too large ({size_mb:.1f} MB > {max_file_mb} MB)"
        )

    with file_path.open("rb") as handle:
        header = handle.read(len(PDF_MAGIC))
    if header != PDF_MAGIC:
        raise ValueError(f"{file_path.name}: invalid PDF signature")


@dataclass(slots=True)
class FileResult:
    """Outcome of translating a single file."""

    source: str
    output_path: str | None
    stats: JobStats
    warnings: list[str] = field(default_factory=list)
    scanned_page_count: int = 0


@dataclass(slots=True)
class JobResult:
    """Outcome of a whole translation job (one or more files)."""

    job_id: str
    files: list[FileResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def stats(self) -> JobStats:
        """Aggregate stats across every file of the job."""
        total = JobStats()
        for file_result in self.files:
            total.total_blocks += file_result.stats.total_blocks
            total.unique_blocks += file_result.stats.unique_blocks
            total.cache_hits += file_result.stats.cache_hits
            total.api_calls += file_result.stats.api_calls
            total.estimated_tokens += file_result.stats.estimated_tokens
        return total


def _batched(items: list, size: int) -> list[list]:
    """Split a list into chunks of at most `size` items."""
    return [items[index : index + size] for index in range(0, len(items), size)]


def _translate_missing(
    texts: list[str],
    translator: Translator,
    cache: TranslationCache,
    src: str,
    tgt: str,
    batch_size: int,
    stats: JobStats,
    warnings: list[str],
    progress_cb: ProgressCallback | None,
    file_label: str,
) -> dict[str, str]:
    """Translate texts absent from the cache, batch by batch; returns text -> translation."""
    batches = _batched(texts, batch_size)
    translations: dict[str, str] = {}

    for index, batch in enumerate(batches, start=1):
        try:
            results = translator.translate_blocks(batch, src, tgt)
            stats.record_api_call(batch)
            for original, translated in zip(batch, results):
                translations[original] = translated
            cache.set_many(dict(zip(batch, results)), src, tgt)
        except Exception as error:  # noqa: BLE001 - a failed batch must not kill the job
            logger.warning("batch %d/%d failed: %s", index, len(batches), type(error).__name__)
            warnings.append(
                f"{file_label}: batch {index}/{len(batches)} failed ({type(error).__name__}), "
                "blocks left in source language"
            )

        if progress_cb:
            progress_cb(f"{file_label}: batch {index}/{len(batches)}")

    return translations


def _insert_scanned_cover_page(pdf_path: Path, scanned_page_numbers: list[int]) -> None:
    """Prepend a warning page listing the pages left untranslated (0-based input)."""
    doc = pymupdf.open(pdf_path)
    try:
        cover = doc.new_page(0)
        lines = [
            "WARNING",
            "",
            "The following pages are scanned pages (image with no extractable",
            "text) and were not translated automatically:",
            "",
            ", ".join(str(number + 1) for number in scanned_page_numbers),
        ]
        cover.insert_textbox(
            pymupdf.Rect(50, 60, cover.rect.width - 50, 300),
            "\n".join(lines),
            fontname="helv",
            fontsize=12,
        )
        tmp_path = pdf_path.with_suffix(".tmp.pdf")
        doc.save(tmp_path, garbage=3, deflate=True)
    finally:
        doc.close()
    tmp_path.replace(pdf_path)


def _translate_scanned_pages(
    source_path: Path,
    out_path: Path,
    scanned_pages: list[int],
    translator: Translator,
    src: str,
    tgt: str,
    stats: JobStats,
    warnings: list[str],
    progress_cb: ProgressCallback | None,
    file_label: str,
) -> list[int]:
    """Translate each scanned page via the multimodal path, in place in out_path.

    Renders the page from the *source* file (the rebuilt output has the same
    geometry but the source is the untouched original), sends it to the model,
    and replaces the page in the output with the translated text.

    Returns the pages that could NOT be translated, so the caller can still
    flag them on the cover page. A page that fails never aborts the job.
    """
    source_doc = pymupdf.open(source_path)
    out_doc = pymupdf.open(out_path)
    failed: list[int] = []

    try:
        # Descending order: rebuilding a page can insert overflow pages after it,
        # which would shift the indices of pages not yet processed.
        for number in sorted(scanned_pages, reverse=True):
            if progress_cb:
                progress_cb(f"{file_label}: scanned page {number + 1}")
            try:
                image = render_page_image(source_doc[number])
                translated = translator.translate_page_image(image, src, tgt)
                stats.record_api_call([translated])

                if not rebuild_scanned_page(out_doc, number, translated, tgt):
                    raise ValueError("empty translation")
            except Exception as error:  # noqa: BLE001 - one page must not kill the job
                logger.warning(
                    "scanned page %d failed: %s", number + 1, type(error).__name__
                )
                warnings.append(
                    f"{file_label}: scanned page {number + 1} could not be translated "
                    f"({type(error).__name__}), left as the original image"
                )
                failed.append(number)

        if len(failed) < len(scanned_pages):
            tmp_path = out_path.with_suffix(".tmp.pdf")
            out_doc.save(tmp_path, garbage=3, deflate=True)
            saved = True
        else:
            saved = False
    finally:
        out_doc.close()
        source_doc.close()

    if saved:
        tmp_path.replace(out_path)

    return sorted(failed)


def _translate_file(
    file_path: Path,
    src: str,
    tgt: str,
    config: dict,
    translator: Translator,
    cache: TranslationCache,
    progress_cb: ProgressCallback | None,
    job_output_dir: Path,
) -> FileResult:
    """Run the full pipeline (extract -> dedup -> translate -> rebuild) on one file."""
    file_label = file_path.name
    warnings: list[str] = []
    stats = JobStats()

    if progress_cb:
        progress_cb(f"{file_label}: extraction")
    document: DocumentData = extract(file_path)

    scanned_pages = [page.number for page in document.pages if page.is_scanned]
    multimodal_enabled = bool(config.get("multimodal_enabled", False))

    # With the multimodal path off, scanned pages are flagged straight away.
    # With it on, the warning depends on which pages actually fail, so it is
    # emitted after the attempt instead.
    if scanned_pages and not multimodal_enabled:
        warnings.append(
            f"{file_label}: {len(scanned_pages)} scanned page(s) not translated "
            f"({', '.join(str(n + 1) for n in scanned_pages)})"
        )

    translatable: list[Block] = [
        block
        for page in document.pages
        if not page.is_scanned
        for block in page.blocks
        if block.is_translatable
    ]
    texts = [block.text for block in translatable]
    stats.record_blocks(texts)

    cache_hits = cache.get_many(texts, src, tgt)
    stats.record_cache_hits(sum(1 for text in texts if text in cache_hits))

    missing = sorted({text for text in texts if text not in cache_hits})
    batch_size = int(config.get("batch_size", 20))
    new_translations = _translate_missing(
        missing, translator, cache, src, tgt, batch_size, stats, warnings, progress_cb, file_label
    )

    text_to_translation = {**cache_hits, **new_translations}
    translations: dict[str, str] = {
        block.id: text_to_translation[block.text]
        for block in translatable
        if block.text in text_to_translation
    }

    out_path = job_output_dir / f"{file_path.stem}_{tgt}.pdf"

    if progress_cb:
        progress_cb(f"{file_label}: rebuilding")
    rebuild_stats = rebuild(file_path, document, translations, out_path, target_lang=tgt)

    if rebuild_stats.blocks_failed:
        warnings.append(
            f"{file_label}: {rebuild_stats.blocks_failed} block(s) could not fit and were "
            "dropped from the output"
        )

    # Scanned pages: translate them through the multimodal path when enabled,
    # then only the ones still untranslated are listed on the cover page.
    untranslated_scans = scanned_pages
    if scanned_pages and multimodal_enabled:
        untranslated_scans = _translate_scanned_pages(
            file_path,
            out_path,
            scanned_pages,
            translator,
            src,
            tgt,
            stats,
            warnings,
            progress_cb,
            file_label,
        )

    if untranslated_scans:
        _insert_scanned_cover_page(out_path, untranslated_scans)

    return FileResult(
        source=str(file_path),
        output_path=str(out_path),
        stats=stats,
        warnings=warnings,
        scanned_page_count=len(scanned_pages),
    )


def run_job(
    files: list[Path],
    src: str,
    tgt: str,
    config: dict,
    progress_cb: ProgressCallback | None = None,
    job_id: str | None = None,
) -> JobResult:
    """Translate every file in `files` from `src` to `tgt`, writing to output/<job_id>/.

    ``job_id`` lets the caller (the web app) impose its own identifier, so the
    output directory matches the one its purge logic removes. When omitted (the
    CLI path), a fresh one is generated.
    """
    for file_path in files:
        _validate_file(file_path, config)

    if job_id is None:
        job_id = uuid.uuid4().hex[:12]
    job_output_dir = OUTPUT_DIR / job_id
    job_output_dir.mkdir(parents=True, exist_ok=True)

    translator = get_translator(config)
    cache = TranslationCache()

    result = JobResult(job_id=job_id)
    total = len(files)
    for index, file_path in enumerate(files, start=1):
        if progress_cb:
            progress_cb(f"file {index}/{total}: {file_path.name}")
        file_result = _translate_file(
            file_path, src, tgt, config, translator, cache, progress_cb, job_output_dir
        )
        result.files.append(file_result)
        result.warnings.extend(file_result.warnings)

    logger.info("job %s done: %d file(s), %s", job_id, total, result.stats.summary())
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI: translate one or more PDFs with the configured (or forced) provider."""
    parser = argparse.ArgumentParser(description="Translate PDF files end to end.")
    parser.add_argument("files", nargs="+", type=Path, help="PDF files to translate")
    parser.add_argument("--src", required=True, help="source language (zh, ja, ko, en, fr)")
    parser.add_argument("--tgt", required=True, help="target language (fr, en)")
    parser.add_argument("--provider", default=None, help="override config.yaml's provider")
    parser.add_argument("--verbose", action="store_true", help="enable info logs")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    app_config.force_utf8_stdout()

    missing = [f for f in args.files if not f.is_file()]
    if missing:
        for f in missing:
            print(f"File not found: {f}", file=sys.stderr)
        return 1

    config = dict(app_config.load_config())
    if args.provider:
        config["provider"] = args.provider

    def progress_cb(message: str) -> None:
        print(f"  -> {message}")

    try:
        result = run_job(args.files, args.src, args.tgt, config, progress_cb=progress_cb)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"\nJob {result.job_id}")
    for file_result in result.files:
        status = file_result.output_path or "FAILED"
        print(f"  {Path(file_result.source).name} -> {status}")
    if result.warnings:
        print("\nWarnings:")
        for warning in result.warnings:
            print(f"  - {warning}")
    print(f"\n{result.stats.summary()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
