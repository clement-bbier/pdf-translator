"""PyMuPDF rebuild: redact original blocks and reinsert replacement text."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app import config as app_config
from app import fonts
from app.extractor import extract
from app.models import Block, DocumentData

logger = logging.getLogger(__name__)

# Font size is reduced by this step while searching for a fit.
FONT_STEP = 0.5

# Absolute floor, whatever font_min_scale says.
MIN_FONT_SIZE = 4.0

# Extra vertical room added on top of the computed line height.
PADDING_RATIO = 0.15


@dataclass(slots=True)
class RebuildStats:
    """Counters for one rebuild run. Never holds document content."""

    blocks_total: int = 0
    blocks_written: int = 0
    blocks_shrunk: int = 0
    blocks_truncated: int = 0
    blocks_failed: int = 0
    glyphs_missing: int = 0
    pages_scanned_skipped: int = 0

    def summary(self) -> str:
        """One-line summary for CLI output."""
        return (
            f"blocks={self.blocks_total} written={self.blocks_written} "
            f"shrunk={self.blocks_shrunk} truncated={self.blocks_truncated} "
            f"failed={self.blocks_failed} missing_glyphs={self.glyphs_missing} "
            f"scanned_skipped={self.pages_scanned_skipped}"
        )


def _rgb_from_packed(color: int) -> tuple[float, float, float]:
    """Unpack a PyMuPDF span color (24-bit sRGB int) into 0..1 floats."""
    return (
        ((color >> 16) & 0xFF) / 255.0,
        ((color >> 8) & 0xFF) / 255.0,
        (color & 0xFF) / 255.0,
    )


def _load_font(fontname: str, fontfile: str | None) -> pymupdf.Font:
    """Build a Font object from a built-in name or a file."""
    if fontfile:
        return pymupdf.Font(fontfile=fontfile)
    return pymupdf.Font(fontname)


def _padded_rect(block: Block, font: pymupdf.Font, page_rect: pymupdf.Rect) -> pymupdf.Rect:
    """Grow the block box downwards so a full line box fits.

    ``get_text`` returns a box tight around the glyphs while ``insert_textbox``
    reserves ascender-to-descender per line. Without this padding every block
    fails to fit, even with its own original text.
    """
    line_height = font.ascender - font.descender
    needed = line_height * block.font_size * max(1, block.line_count)

    rect = pymupdf.Rect(block.bbox)
    if rect.height < needed:
        rect.y1 = rect.y0 + needed
    rect.y1 += block.font_size * PADDING_RATIO
    return rect & page_rect


def _fits(
    page_rect: pymupdf.Rect,
    rect: pymupdf.Rect,
    text: str,
    font: pymupdf.Font,
    size: float,
) -> bool:
    """Measure whether `text` fits `rect` at `size`, without drawing anything.

    insert_textbox commits its draw to the page's content stream as soon as a
    call succeeds — even one made purely to test a size or a truncation length
    that is later discarded. Probing with it therefore leaves permanent, partial
    artifacts behind for every candidate tried, not just the one kept. TextWriter
    is a free-standing buffer: fill_textbox lays text out against it only, so
    probing here has no effect on the page.
    """
    writer = pymupdf.TextWriter(page_rect)
    overflow = writer.fill_textbox(rect, text, font=font, fontsize=size)
    return not overflow


def _truncate_to_fit(
    page: pymupdf.Page,
    page_rect: pymupdf.Rect,
    rect: pymupdf.Rect,
    text: str,
    fontname: str,
    fontfile: str | None,
    font: pymupdf.Font,
    size: float,
    color: tuple[float, float, float],
) -> str | None:
    """Find by bisection the longest prefix that fits, suffixed with '...'.

    Every candidate is measured with `_fits` (no page side effect) during the
    search. TextWriter's layout engine and insert_textbox's do not always agree
    at the margin (different internal wrapping math), so the bisection's winner
    is only a strong hint: it is verified with a real (but not yet committed —
    insert_textbox only commits on success) draw, and if that disagrees, shorter
    already-measured-as-fitting candidates are retried in turn. Exactly one
    successful real draw happens, for the candidate finally kept.
    """
    low, high = 0, len(text)
    fitting_lengths: list[int] = []

    while low <= high:
        middle = (low + high) // 2
        candidate = text[:middle].rstrip() + "..."
        if _fits(page_rect, rect, candidate, font, size):
            fitting_lengths.append(middle)
            low = middle + 1
        else:
            high = middle - 1

    for middle in sorted(fitting_lengths, reverse=True):
        candidate = text[:middle].rstrip() + "..."
        rc = page.insert_textbox(
            rect, candidate, fontname=fontname, fontfile=fontfile, fontsize=size, color=color
        )
        if rc >= 0:
            return candidate
    return None


def _fit_textbox(
    page: pymupdf.Page,
    page_rect: pymupdf.Rect,
    rect: pymupdf.Rect,
    text: str,
    fontname: str,
    fontfile: str | None,
    font: pymupdf.Font,
    size: float,
    min_size: float,
    color: tuple[float, float, float],
) -> tuple[bool, bool, bool]:
    """Write text, shrinking then truncating as needed.

    Returns (written, shrunk, truncated). Each candidate size is measured with
    `_fits` (no page side effect); only a size that measures as fitting is drawn
    for real. TextWriter's layout engine and insert_textbox's do not always agree
    at the margin (different internal wrapping math): when the real draw of a
    "fits" size fails, that is not a stacked artifact (insert_textbox only
    commits on success), so the loop simply keeps shrinking.
    """
    current = size
    while current >= min_size - 1e-6:
        if _fits(page_rect, rect, text, font, current):
            rc = page.insert_textbox(
                rect, text, fontname=fontname, fontfile=fontfile, fontsize=current, color=color
            )
            if rc >= 0:
                return True, current < size, False
        current -= FONT_STEP

    floor = max(min_size, MIN_FONT_SIZE)
    truncated = _truncate_to_fit(page, page_rect, rect, text, fontname, fontfile, font, floor, color)
    if truncated is None:
        return False, True, False
    return True, True, True


def rebuild(
    pdf_path: str | Path,
    doc_data: DocumentData,
    translations: dict[str, str],
    out_path: str | Path,
    *,
    target_lang: str = "fr",
    font_path: str | None = None,
    font_min_scale: float | None = None,
) -> RebuildStats:
    """Replace each translated block in the PDF and save a new file.

    Blocks absent from ``translations`` keep their original text untouched.
    """
    if font_min_scale is None:
        font_min_scale = float(app_config.get("font_min_scale", 0.6))

    stats = RebuildStats()
    doc = pymupdf.open(pdf_path)

    try:
        for page_data in doc_data.pages:
            if page_data.is_scanned:
                stats.pages_scanned_skipped += 1
                continue

            page = doc[page_data.number]
            targets = [block for block in page_data.blocks if block.id in translations]
            if not targets:
                continue
            stats.blocks_total += len(targets)

            # Pass 1: erase. apply_redactions rewrites the content stream, so it
            # must run before any new text is inserted.
            for block in targets:
                page.add_redact_annot(pymupdf.Rect(block.bbox))
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )

            # Pass 2: write the replacement text.
            for block in targets:
                text = translations[block.id]
                if not text.strip():
                    continue
                _write_block(page, block, text, stats, target_lang, font_path, font_min_scale)

        doc.save(out_path, garbage=3, deflate=True)
    finally:
        doc.close()

    # Counters only: the output name derives from the user's file name.
    logger.info("rebuild: %s", stats.summary())
    return stats


def _write_block(
    page: pymupdf.Page,
    block: Block,
    text: str,
    stats: RebuildStats,
    target_lang: str,
    font_path: str | None,
    font_min_scale: float,
) -> None:
    """Write one block, choosing the font from the text's own script."""
    # The script actually present wins over the requested language: a block left
    # in the source language must not be written with the target font.
    script = fonts.dominant_script(text)
    if font_path:
        fontname, fontfile = "custom", font_path
    elif script == fonts.LATIN:
        fontname, fontfile = fonts.select_font(
            target_lang if fonts.LANG_SCRIPTS.get(target_lang) == fonts.LATIN else "fr",
            bold=block.is_bold,
            italic=block.is_italic,
        )
    else:
        fontname, fontfile = fonts.font_for_script(script), None

    # Prefer a font that renders the text as written; only fold typographic
    # characters to Latin-1 when no built-in font can display them.
    fontname, fontfile = fonts.best_font_for(text, fontname, fontfile)

    missing = fonts.missing_chars(text, fontname, fontfile)
    if missing and fontfile is None and fontname in fonts.BASE14_STYLES.values():
        text, replaced = fonts.sanitize_latin1(text)
        if replaced:
            stats.glyphs_missing += replaced
            logger.warning("block %s: %d unsupported characters", block.id, replaced)
        missing = fonts.missing_chars(text, fontname, fontfile)

    if missing:
        stats.glyphs_missing += len(missing)
        logger.warning(
            "block %s: %d characters cannot be rendered by %s",
            block.id,
            len(missing),
            fontname,
        )

    font = _load_font(fontname, fontfile)
    rect = _padded_rect(block, font, page.rect)
    min_size = max(block.font_size * font_min_scale, MIN_FONT_SIZE)
    color = _rgb_from_packed(block.color)

    written, shrunk, truncated = _fit_textbox(
        page, page.rect, rect, text, fontname, fontfile, font, block.font_size, min_size, color
    )

    if not written:
        stats.blocks_failed += 1
        logger.warning("block %s: could not fit %d characters", block.id, len(text))
        return

    stats.blocks_written += 1
    if shrunk:
        stats.blocks_shrunk += 1
    if truncated:
        stats.blocks_truncated += 1
        logger.warning("block %s truncated (%d chars)", block.id, len(text))


def main(argv: list[str] | None = None) -> int:
    """CLI: rebuild a PDF, reinserting the source text when no translation is given."""
    parser = argparse.ArgumentParser(
        description="Rebuild a PDF by replacing its text blocks."
    )
    parser.add_argument("pdf", type=Path, help="path to the source PDF")
    parser.add_argument("-o", "--output", type=Path, required=True, help="output PDF")
    parser.add_argument("--target-lang", default="fr", help="target language (fr, en, zh, ja, ko)")
    parser.add_argument("--font-path", default=None, help="optional font file to embed")
    parser.add_argument("--verbose", action="store_true", help="enable info logs")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    app_config.force_utf8_stdout()

    if not args.pdf.is_file():
        print(f"File not found: {args.pdf}", file=sys.stderr)
        return 1

    document = extract(args.pdf)
    # Identity mode: reinsert the source text to isolate layout regressions.
    translations = {block.id: block.text for block in document.all_blocks()}

    args.output.parent.mkdir(parents=True, exist_ok=True)
    stats = rebuild(
        args.pdf,
        document,
        translations,
        args.output,
        target_lang=args.target_lang,
        font_path=args.font_path,
    )

    print(f"Rebuilt : {args.output}")
    print(f"Stats   : {stats.summary()}")
    return 0 if stats.blocks_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
