"""PyMuPDF extraction: text blocks with coordinates and scanned-page detection."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict
from pathlib import Path

import pymupdf

from app import config as app_config
from app.models import BBox, Block, DocumentData, PageData

logger = logging.getLogger(__name__)

# A block is considered a scanned page if an image covers at least this
# fraction of the page area and no text was extracted.
SCANNED_IMAGE_COVERAGE = 0.6

# Two lines belong to different columns when they overlap vertically by more
# than this ratio while not overlapping horizontally at all.
COLUMN_OVERLAP_RATIO = 0.6

# Span flag bits (PyMuPDF).
_FLAG_ITALIC = 2
_FLAG_BOLD = 16

_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

_NON_TRANSLATABLE_RE = re.compile(
    r"""^(?:
          [\d\s.,:;/\\'’%€$£¥+\-()\[\]]+          # numbers, amounts, percentages
        | \d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}       # 12/03/2024
        | \d{4}[/.\-]\d{1,2}[/.\-]\d{1,2}         # 2024-03-12
        | (?:page|p\.)\s*\d+(?:\s*/\s*\d+)?       # Page 1/2
        | [A-Z]{2,}[-_ ]?\d[\w\-/]*               # REF-2024-0871, ISO9001
        | [ivxlcdm]+\.?                           # roman numerals
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


def is_translatable(text: str) -> bool:
    """Return True when a block carries meaning worth translating."""
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    # No letter at all in any script (covers "1 250,00" and pure punctuation).
    if not _LETTER_RE.search(stripped):
        return False
    if _NON_TRANSLATABLE_RE.match(stripped):
        return False
    return True


def _union_bbox(boxes: list[BBox]) -> BBox:
    """Return the smallest box containing all given boxes."""
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _split_columns(lines: list[dict]) -> list[list[dict]]:
    """Group lines into columns.

    Table cells sharing a baseline arrive as one block; rewriting them as a
    single run would destroy the table, so lines that sit side by side are
    separated.
    """
    if len(lines) <= 1:
        return [list(lines)]

    ordered = sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))
    groups: list[list[dict]] = [[ordered[0]]]

    for line in ordered[1:]:
        previous = groups[-1][-1]
        prev_box, box = previous["bbox"], line["bbox"]

        overlap = min(prev_box[3], box[3]) - max(prev_box[1], box[1])
        min_height = min(prev_box[3] - prev_box[1], box[3] - box[1]) or 1.0
        side_by_side = overlap / min_height > COLUMN_OVERLAP_RATIO
        disjoint = box[0] >= prev_box[2] or box[2] <= prev_box[0]

        if side_by_side and disjoint:
            groups.append([line])
        else:
            groups[-1].append(line)
    return groups


def _line_text(line: dict) -> str:
    """Concatenate the spans of a line."""
    return "".join(span["text"] for span in line["spans"])


def _block_style(lines: list[dict]) -> tuple[float, str, bool, bool]:
    """Return (font_size, font_name, bold, italic) weighted by character count."""
    weights: dict[tuple[float, str, bool, bool], int] = {}
    for line in lines:
        for span in line["spans"]:
            length = len(span["text"].strip())
            if not length:
                continue
            name = span["font"]
            lowered = name.lower()
            key = (
                round(float(span["size"]), 1),
                name,
                bool(span["flags"] & _FLAG_BOLD) or "bold" in lowered,
                bool(span["flags"] & _FLAG_ITALIC)
                or "italic" in lowered
                or "oblique" in lowered,
            )
            weights[key] = weights.get(key, 0) + length

    if not weights:
        return 11.0, "helv", False, False
    return max(weights, key=lambda key: weights[key])


def _block_color(lines: list[dict]) -> int:
    """Return the packed sRGB color covering most characters of the block."""
    weights: dict[int, int] = {}
    for line in lines:
        for span in line["spans"]:
            length = len(span["text"].strip())
            if not length:
                continue
            color = int(span["color"])
            weights[color] = weights.get(color, 0) + length

    if not weights:
        return 0
    return max(weights, key=lambda key: weights[key])


def _is_scanned(text_blocks: list[dict], page: pymupdf.Page) -> bool:
    """Detect a scanned page: no extractable text plus a near-full-page image.

    Image placement comes from ``get_image_info`` because the text extraction
    runs with TEXTFLAGS_TEXT, which drops image blocks.
    """
    has_text = any(_line_text(line).strip() for block in text_blocks for line in block["lines"])
    if has_text:
        return False

    page_area = page.rect.get_area() or 1.0
    for info in page.get_image_info():
        box = info["bbox"]
        area = (box[2] - box[0]) * (box[3] - box[1])
        if area / page_area >= SCANNED_IMAGE_COVERAGE:
            return True
    return False


def _extract_page(page: pymupdf.Page, index: int) -> PageData:
    """Extract one page into a PageData."""
    content = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
    text_blocks = [block for block in content["blocks"] if block["type"] == 0]

    page_data = PageData(
        number=index,
        width=float(content["width"]),
        height=float(content["height"]),
        is_scanned=_is_scanned(text_blocks, page),
    )

    for block in text_blocks:
        groups = _split_columns(block["lines"])
        for column, lines in enumerate(groups):
            text = " ".join(
                _line_text(line).strip() for line in lines if _line_text(line).strip()
            )
            text = re.sub(r"[ \t]+", " ", text).strip()
            if not text:
                continue

            size, font_name, bold, italic = _block_style(lines)
            suffix = "" if len(groups) == 1 else f"c{column}"
            page_data.blocks.append(
                Block(
                    id=f"p{index}_b{block['number']}{suffix}",
                    text=text,
                    bbox=_union_bbox([line["bbox"] for line in lines]),
                    font_size=size,
                    font_name=font_name,
                    is_bold=bold,
                    is_italic=italic,
                    is_translatable=is_translatable(text),
                    line_count=len(lines),
                    color=_block_color(lines),
                )
            )

    return page_data


def extract(pdf_path: str | Path) -> DocumentData:
    """Extract every text block of a PDF with its layout information."""
    path = Path(pdf_path)
    doc = pymupdf.open(path)
    try:
        metadata = doc.metadata or {}
        document = DocumentData(
            path=str(path),
            page_count=doc.page_count,
            title=metadata.get("title") or None,
            author=metadata.get("author") or None,
        )
        for index, page in enumerate(doc):
            document.pages.append(_extract_page(page, index))
    finally:
        doc.close()

    # Counters only: the file name is user content and must stay out of logs.
    logger.info(
        "extracted: %d pages, %d blocks, %d translatable",
        document.page_count,
        len(document.all_blocks()),
        len(document.translatable_blocks()),
    )
    return document


def _print_report(document: DocumentData) -> None:
    """Print the extracted blocks in a human-readable form."""
    blocks = document.all_blocks()
    scanned = sum(1 for page in document.pages if page.is_scanned)
    print(f"Document : {Path(document.path).name}")
    print(f"Title    : {document.title or '-'}   Author : {document.author or '-'}")
    print(
        f"Pages    : {document.page_count}   Blocks : {len(blocks)}   "
        f"Translatable : {len(document.translatable_blocks())}   Scanned : {scanned}"
    )

    for page in document.pages:
        flag = " [SCANNED]" if page.is_scanned else ""
        print(f"\n--- Page {page.number + 1} ({page.width:.0f} x {page.height:.0f}){flag} ---")
        for block in page.blocks:
            style = f"{block.font_size:.1f} {block.font_name}"
            if block.is_bold:
                style += "-bold"
            if block.is_italic:
                style += "-italic"
            mark = "T" if block.is_translatable else "-"
            box = "(" + ",".join(f"{value:.0f}" for value in block.bbox) + ")"
            color = f"#{block.color:06x}"
            preview = block.text if len(block.text) <= 60 else block.text[:57] + "..."
            print(f"  {block.id:<12} [{style:<22}] {mark} {color} {box:<24} {preview}")


def main(argv: list[str] | None = None) -> int:
    """CLI: display the blocks extracted from a PDF."""
    parser = argparse.ArgumentParser(description="Extract text blocks from a PDF.")
    parser.add_argument("pdf", type=Path, help="path to the PDF file")
    parser.add_argument("--json", action="store_true", help="output JSON")
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

    if args.json:
        print(json.dumps(asdict(document), ensure_ascii=False, indent=2))
        return 0

    _print_report(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
