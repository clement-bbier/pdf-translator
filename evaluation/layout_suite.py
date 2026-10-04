"""Layout suite: how well the rebuilder fits translated text back into the page.

Every PDF in tests/fixtures/ goes through extract -> MockTranslator -> rebuild.
The mock grows each block to ~1.4x its length, a realistic CJK -> FR/EN
expansion, so the suite measures the layout engine (shrink, truncate, drop,
missing glyphs) independently of any model. Fully deterministic: a change in
these numbers is a change in the code, never noise.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.extractor import extract
from app.rebuilder import rebuild
from app.translator import MockTranslator

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

NAME = "layout"

# metric -> True when higher is better.
DIRECTIONS: dict[str, bool] = {
    "blocks_total": True,
    "written_ratio": True,
    "shrunk_ratio": False,
    "truncated_ratio": False,
    "failed_blocks": False,
    "missing_glyphs": False,
}


def run() -> dict[str, float]:
    """Return the suite's aggregate metrics over every fixture."""
    translator = MockTranslator()
    totals = {"total": 0, "written": 0, "shrunk": 0, "truncated": 0, "failed": 0, "glyphs": 0}

    with tempfile.TemporaryDirectory() as tmp:
        for pdf in sorted(FIXTURES_DIR.glob("*.pdf")):
            document = extract(pdf)
            blocks = [
                block
                for page in document.pages
                if not page.is_scanned
                for block in page.blocks
                if block.is_translatable
            ]
            translated = translator.translate_blocks([block.text for block in blocks], "auto", "en")
            translations = {block.id: text for block, text in zip(blocks, translated, strict=True)}
            stats = rebuild(
                pdf,
                document,
                translations,
                Path(tmp) / pdf.name,
                target_lang="en",
                font_min_scale=0.6,
            )
            totals["total"] += stats.blocks_total
            totals["written"] += stats.blocks_written
            totals["shrunk"] += stats.blocks_shrunk
            totals["truncated"] += stats.blocks_truncated
            totals["failed"] += stats.blocks_failed
            totals["glyphs"] += stats.glyphs_missing

    total = totals["total"] or 1
    return {
        "blocks_total": float(totals["total"]),
        "written_ratio": round(totals["written"] / total, 4),
        "shrunk_ratio": round(totals["shrunk"] / total, 4),
        "truncated_ratio": round(totals["truncated"] / total, 4),
        "failed_blocks": float(totals["failed"]),
        "missing_glyphs": float(totals["glyphs"]),
    }
