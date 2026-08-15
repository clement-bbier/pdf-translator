"""Generate the test fixtures with PyMuPDF.

All content is invented. Never commit a real contract here.
Run: python tests/make_fixtures.py [--force]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymupdf

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# The Base-14 latin fonts cannot encode ’ € … œ, so the fixtures write French
# text with a built-in CJK font, which covers the full latin range. Without this
# the fixture would contain "?" and could not test special-character handling.
_FR_FONT = "japan"

# French text exercising the characters Base-14 fonts cannot encode.
_FR_PARAGRAPH = (
    "L’échéance du présent contrat est fixée au 12/03/2024. "
    "Le prestataire s’engage à fournir les livrables convenus, "
    "ça coûte 1 250,00 € par mois… "
    "L’œuvre reste la propriété du client."
)
_FR_PARAGRAPH_2 = (
    "En cas de manquement, les parties conviennent d’une période "
    "de régularisation de trente (30) jours calendaires."
)

_ZH_TITLE = "合同终止条款"
_ZH_BODY = (
    "第一条 本合同自双方签署之日"
    "起生效，双方应当严格遵守本"
    "合同的各项约定。"
)
_ZH_BODY_2 = (
    "第二条 任何一方不得擅自变更"
    "或解除本合同。"
)


def _table(page: pymupdf.Page, top: float) -> None:
    """Draw a simple table: positioned cells plus rules."""
    rows = [
        ("Date", "Montant", "Référence"),
        ("12/03/2024", "1 250,00", "REF-2024-0871"),
        ("15/04/2024", "980,50", "REF-2024-0912"),
    ]
    columns = (50.0, 220.0, 340.0)
    row_height = 22.0

    for row_index, row in enumerate(rows):
        y = top + row_index * row_height
        for column_index, cell in enumerate(row):
            bold = row_index == 0
            page.insert_text(
                (columns[column_index], y),
                cell,
                fontname="hebo" if bold else "helv",
                fontsize=10,
            )
        page.draw_line(
            pymupdf.Point(45, y + 6), pymupdf.Point(500, y + 6), color=(0.6, 0.6, 0.6), width=0.5
        )


def make_simple(path: Path) -> None:
    """Two-page French document: title, paragraphs, list, table, footer."""
    doc = pymupdf.open()

    page = doc.new_page()
    page.insert_text((50, 60), "CONTRAT DE PRESTATION", fontname="hebo", fontsize=18)
    page.insert_textbox(
        pymupdf.Rect(50, 90, 545, 170), _FR_PARAGRAPH, fontname=_FR_FONT, fontsize=11
    )
    page.insert_textbox(
        pymupdf.Rect(50, 180, 545, 230), _FR_PARAGRAPH_2, fontname=_FR_FONT, fontsize=11
    )
    for index, item in enumerate(
        ("Livraison des documents sources", "Validation par le client", "Facturation mensuelle")
    ):
        page.insert_text((60, 250 + index * 16), f"- {item}", fontname="helv", fontsize=10)
    _table(page, 320)
    page.insert_text(
        (50, 300), "Résiliation immédiate en cas de non-paiement.",
        fontname=_FR_FONT, fontsize=11, color=(0.8, 0, 0),
    )
    page.insert_text((50, 800), "Page 1/2", fontname="helv", fontsize=9)
    page.insert_text((450, 800), "REF-2024-0871", fontname="helv", fontsize=9)

    page = doc.new_page()
    page.insert_text((50, 60), "ANNEXE TECHNIQUE", fontname="hebo", fontsize=16)
    page.insert_textbox(
        pymupdf.Rect(50, 90, 545, 200),
        _FR_PARAGRAPH_2 + " " + _FR_PARAGRAPH,
        fontname=_FR_FONT,
        fontsize=11,
    )
    page.insert_text((50, 800), "Page 2/2", fontname="helv", fontsize=9)

    doc.set_metadata({"title": "Contrat de prestation", "author": "Service juridique"})
    doc.save(path, garbage=3, deflate=True)
    doc.close()


def make_contract_zh(path: Path) -> None:
    """One-page Chinese contract: the real target case."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), _ZH_TITLE, fontname="china-s", fontsize=18)
    page.insert_textbox(pymupdf.Rect(50, 90, 545, 180), _ZH_BODY, fontname="china-s", fontsize=11)
    page.insert_textbox(pymupdf.Rect(50, 190, 545, 260), _ZH_BODY_2, fontname="china-s", fontsize=11)
    page.insert_text((50, 800), "2024/03/12", fontname="helv", fontsize=9)
    doc.set_metadata({"title": _ZH_TITLE, "author": "法务部"})
    doc.save(path, garbage=3, deflate=True)
    doc.close()


def make_with_image(path: Path) -> None:
    """One page with an in-memory image plus text over it."""
    doc = pymupdf.open()
    page = doc.new_page()

    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 240, 160))
    pixmap.set_rect(pixmap.irect, (200, 220, 240))
    pixmap.set_rect(pymupdf.IRect(20, 20, 220, 60), (120, 160, 210))
    page.insert_image(pymupdf.Rect(50, 300, 290, 460), pixmap=pixmap)

    page.insert_text((50, 60), "RAPPORT ILLUSTRÉ", fontname="hebo", fontsize=16)
    page.insert_textbox(
        pymupdf.Rect(50, 90, 545, 180), _FR_PARAGRAPH, fontname=_FR_FONT, fontsize=11
    )
    page.insert_text((50, 500), "Figure 1 : schéma de principe", fontname="heit", fontsize=9)

    doc.set_metadata({"title": "Rapport illustré", "author": "Service technique"})
    doc.save(path, garbage=3, deflate=True)
    doc.close()


def make_text_over_image(path: Path) -> None:
    """Text sitting on top of an image: the redaction stress case.

    With PyMuPDF's default redaction settings this page comes back with white
    rectangles punched through the image where the text used to be.
    """
    doc = pymupdf.open()
    page = doc.new_page()

    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 300))
    pixmap.set_rect(pixmap.irect, (230, 180, 120))
    pixmap.set_rect(pymupdf.IRect(0, 0, 400, 60), (60, 90, 160))
    page.insert_image(pymupdf.Rect(50, 80, 500, 420), pixmap=pixmap)

    page.insert_text((70, 120), "TITRE SUR IMAGE", fontname="hebo", fontsize=20, color=(1, 1, 1))
    page.insert_textbox(
        pymupdf.Rect(70, 200, 480, 300),
        "Bandeau illustré avec du texte superposé sur le fond.",
        fontname=_FR_FONT,
        fontsize=12,
    )
    page.draw_rect(pymupdf.Rect(50, 80, 500, 420), color=(0.2, 0.2, 0.2), width=1)

    doc.set_metadata({"title": "Texte sur image", "author": "Service communication"})
    doc.save(path, garbage=3, deflate=True)
    doc.close()


def make_dense_layout(path: Path) -> None:
    """Dense multi-column marketing-style page: title, callout box, columns, short bullets.

    Every text box is deliberately small (tight height, narrow columns) so a 1.4x
    growth (MockTranslator) cannot fit at the original font size — this is the
    layout shape that triggers the rebuilder's shrink/truncate fallback path.
    """
    doc = pymupdf.open()
    page = doc.new_page()

    page.insert_textbox(
        pymupdf.Rect(40, 30, 555, 50), "PRODUCT LAUNCH BRIEFING", fontname="hebo", fontsize=14
    )

    # Callout box: short bbox height relative to the text it holds.
    page.draw_rect(pymupdf.Rect(40, 60, 300, 82), color=(0.3, 0.3, 0.3), width=0.5)
    page.insert_textbox(
        pymupdf.Rect(44, 62, 296, 80),
        "Keep reading. You'll see how this gets you speaking fluently.",
        fontname="helv",
        fontsize=9,
    )

    # Two narrow columns of short paragraphs, tight vertical boxes.
    left_col = [
        "Fast onboarding for new teams.",
        "Works offline once installed.",
        "No external dependency required.",
    ]
    right_col = [
        "Consistent output across runs.",
        "Reviewed by native speakers.",
        "Ready for internal rollout.",
    ]
    for index, text in enumerate(left_col):
        top = 95 + index * 26
        page.insert_textbox(
            pymupdf.Rect(40, top, 190, top + 16), text, fontname="helv", fontsize=8
        )
    for index, text in enumerate(right_col):
        top = 95 + index * 26
        page.insert_textbox(
            pymupdf.Rect(210, top, 360, top + 16), text, fontname="helv", fontsize=8
        )

    # Short bullet list, narrow bboxes.
    bullets = ["Setup", "Translate", "Review", "Export"]
    for index, item in enumerate(bullets):
        top = 200 + index * 14
        page.insert_textbox(
            pymupdf.Rect(40, top, 140, top + 12), f"- {item}", fontname="helv", fontsize=8
        )

    doc.set_metadata({"title": "Dense marketing layout", "author": "Marketing"})
    doc.save(path, garbage=3, deflate=True)
    doc.close()


def make_scanned(path: Path) -> None:
    """A page holding only a full-page image, as a scan would produce."""
    doc = pymupdf.open()
    page = doc.new_page()

    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 850))
    pixmap.set_rect(pixmap.irect, (245, 243, 236))
    # Grey bars standing in for scanned text lines: no extractable text.
    for index in range(12):
        top = 80 + index * 40
        pixmap.set_rect(pymupdf.IRect(60, top, 540, top + 14), (90, 90, 90))
    page.insert_image(page.rect, pixmap=pixmap)

    doc.set_metadata({"title": "Document scanné", "author": "Numérisation"})
    doc.save(path, garbage=3, deflate=True)
    doc.close()


FIXTURES = {
    "simple.pdf": make_simple,
    "contract_zh.pdf": make_contract_zh,
    "with_image.pdf": make_with_image,
    "text_over_image.pdf": make_text_over_image,
    "scanned.pdf": make_scanned,
    "dense_layout.pdf": make_dense_layout,
}


def main(argv: list[str] | None = None) -> int:
    """Generate every fixture."""
    parser = argparse.ArgumentParser(description="Generate the test fixtures.")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args(argv)

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for name, builder in FIXTURES.items():
        target = FIXTURES_DIR / name
        if target.exists() and not args.force:
            print(f"skip   {name} (exists, use --force)")
            continue
        builder(target)
        print(f"create {name} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
