"""Multimodal path for scanned pages: render a page to PNG, rebuild it as text.

A scanned page carries no extractable text, so the block-by-block path in
rebuilder.py does not apply: there are no bboxes to redact and refill. Here the
page is rasterised, handed to a multimodal model as an image, and the returned
translation is written back as a plain, freshly laid out text page.

Layout fidelity is deliberately abandoned on this path — the original is an
image, so there is nothing to preserve positionally. The output is a clean,
readable, selectable text page (PLAN.md phase 6 calls this "simplified output").
That is the trade-off: content is recovered, geometry is not.

CLI: python -m app.scanned file.pdf --page 0 -o page.png
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pymupdf

from app import config as app_config
from app import fonts

logger = logging.getLogger(__name__)

# Rasterisation resolution for the page image sent to the model.
# Measured on tests/fixtures/scanned.pdf (A4): 72 dpi is too blurry for small
# glyphs, 150 dpi gives 1240x1755 px (~13 KB PNG, ~17 KB base64) which is the
# usual legibility threshold for contract text, and 200 dpi costs ~60% more
# image tokens for no readable gain on body text.
SCAN_DPI = 150

# Page geometry for the rebuilt text page, in PDF points (72 per inch).
MARGIN = 56.0  # ~2 cm
BODY_FONT_SIZE = 11.0
LINE_SPACING = 1.35
PARAGRAPH_GAP = 6.0


def render_page_image(page: pymupdf.Page, dpi: int = SCAN_DPI) -> bytes:
    """Rasterise a page to PNG bytes, ready to be sent to a multimodal model."""
    pixmap = page.get_pixmap(dpi=dpi)
    return pixmap.tobytes("png")


def _select_font(text: str, target_lang: str) -> tuple[str, str | None]:
    """Pick a font that can actually render the translated text.

    Reuses the same helper sequence as rebuilder._write_block: the script
    present in the text wins over the requested language, so a page left in the
    source language is not written with a font that cannot encode it.
    """
    script = fonts.dominant_script(text)
    if script == fonts.LATIN:
        fontname, fontfile = fonts.select_font(
            target_lang if fonts.LANG_SCRIPTS.get(target_lang) == fonts.LATIN else "fr"
        )
    else:
        fontname, fontfile = fonts.font_for_script(script), None

    return fonts.best_font_for(text, fontname, fontfile)


def _paragraphs(text: str) -> list[str]:
    """Split the model's answer into paragraphs on blank lines."""
    blocks = [block.strip() for block in text.replace("\r\n", "\n").split("\n\n")]
    return [block for block in blocks if block]


def rebuild_scanned_page(
    doc: pymupdf.Document,
    page_number: int,
    text: str,
    target_lang: str = "fr",
) -> bool:
    """Replace a scanned page's content with the translated text.

    The original image is dropped: the page becomes clean, selectable text.
    Overflow continues onto freshly inserted pages so nothing is silently lost.

    Returns False when there is nothing to write or the text could not be
    placed, so the caller can fall back to leaving the page untranslated.
    """
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return False

    fontname, fontfile = _select_font(text, target_lang)

    missing = fonts.missing_chars(text, fontname, fontfile)
    if missing and fontfile is None and fontname in fonts.BASE14_STYLES.values():
        text, replaced = fonts.sanitize_latin1(text)
        if replaced:
            logger.warning("scanned page %d: %d unsupported characters", page_number, replaced)
        paragraphs = _paragraphs(text)

    page = doc[page_number]
    rect = page.rect
    # Wipe the scanned image: this page is being replaced, not annotated.
    page.clean_contents()
    doc.delete_page(page_number)
    page = doc.new_page(page_number, width=rect.width, height=rect.height)

    writer_rect = pymupdf.Rect(
        MARGIN, MARGIN, rect.width - MARGIN, rect.height - MARGIN
    )
    font = pymupdf.Font(fontfile=fontfile) if fontfile else pymupdf.Font(fontname)
    line_height = BODY_FONT_SIZE * LINE_SPACING

    cursor = writer_rect.y0
    written_any = False

    for paragraph in paragraphs:
        for line in _wrap(paragraph, font, BODY_FONT_SIZE, writer_rect.width):
            if cursor + line_height > writer_rect.y1:
                # Continue on a new page rather than dropping the remainder.
                page = doc.new_page(
                    doc.page_count if page_number + 1 >= doc.page_count else page_number + 1,
                    width=rect.width,
                    height=rect.height,
                )
                page_number = page.number
                cursor = writer_rect.y0

            page.insert_text(
                pymupdf.Point(writer_rect.x0, cursor + BODY_FONT_SIZE),
                line,
                fontname=fontname,
                fontfile=fontfile,
                fontsize=BODY_FONT_SIZE,
            )
            written_any = True
            cursor += line_height

        cursor += PARAGRAPH_GAP

    return written_any


def _wrap(paragraph: str, font: pymupdf.Font, size: float, width: float) -> list[str]:
    """Greedy word wrap; falls back to per-character wrapping for CJK runs.

    CJK text has no spaces, so a word-based wrap would produce one enormous
    "word" that overflows the page: such a run is broken character by character.
    """
    words = paragraph.split()
    if not words:
        return []

    lines: list[str] = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if font.text_length(candidate, size) <= width:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = ""

        if font.text_length(word, size) <= width:
            current = word
            continue

        # A single token wider than the page (typically an unspaced CJK run).
        for char in word:
            candidate = current + char
            if font.text_length(candidate, size) > width and current:
                lines.append(current)
                current = char
            else:
                current = candidate

    if current:
        lines.append(current)
    return lines


def main(argv: list[str] | None = None) -> int:
    """CLI: dump the PNG a scanned page would be sent to the model as."""
    parser = argparse.ArgumentParser(
        description="Render a PDF page to PNG, as sent to a multimodal model."
    )
    parser.add_argument("pdf", type=Path, help="path to the PDF file")
    parser.add_argument("--page", type=int, default=0, help="0-based page number")
    parser.add_argument("--dpi", type=int, default=SCAN_DPI, help="render resolution")
    parser.add_argument("-o", "--output", type=Path, required=True, help="output PNG")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    app_config.force_utf8_stdout()

    if not args.pdf.is_file():
        print(f"File not found: {args.pdf}", file=sys.stderr)
        return 1

    doc = pymupdf.open(args.pdf)
    try:
        if not 0 <= args.page < doc.page_count:
            print(f"Page {args.page} out of range (0..{doc.page_count - 1})", file=sys.stderr)
            return 1
        image = render_page_image(doc[args.page], dpi=args.dpi)
    finally:
        doc.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(image)
    print(f"Rendered page {args.page} at {args.dpi} dpi -> {args.output} ({len(image)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
