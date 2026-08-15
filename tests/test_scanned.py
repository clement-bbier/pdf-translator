"""Unit tests for the scanned-page multimodal path (scanned.py).

No network and no model: the rendering and the page rebuild are tested
directly. Run: python tests/test_scanned.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from app import config as app_config  # noqa: E402
from app import fonts  # noqa: E402
from app.scanned import SCAN_DPI, rebuild_scanned_page, render_page_image  # noqa: E402
from tests import make_fixtures  # noqa: E402

FAILURES: list[str] = []

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def _scanned_fixture() -> Path:
    return make_fixtures.FIXTURES_DIR / "scanned.pdf"


# --------------------------------------------------------------------------- #
# 1. rendering
# --------------------------------------------------------------------------- #


def test_render_page_image_produces_valid_png() -> None:
    """A scanned page renders to real PNG bytes at the configured resolution."""
    doc = pymupdf.open(_scanned_fixture())
    try:
        page = doc[0]
        expected_width = round(page.rect.width * SCAN_DPI / 72)
        image = render_page_image(page)
    finally:
        doc.close()

    check(image.startswith(PNG_MAGIC), "render_page_image should return PNG bytes")
    check(len(image) > 1000, f"PNG looks too small to be a page: {len(image)} bytes")

    # Decoding the PNG proves it is a real image, not just PNG-prefixed, and
    # exposes the pixel dimensions (a Page rect would be in points, not pixels).
    decoded = pymupdf.Pixmap(image)
    check(
        abs(decoded.width - expected_width) <= 2,
        f"expected ~{expected_width}px wide at {SCAN_DPI} dpi, got {decoded.width}",
    )


def test_render_page_image_respects_dpi() -> None:
    """A higher dpi yields a larger image; the resolution argument is honoured."""
    doc = pymupdf.open(_scanned_fixture())
    try:
        small = render_page_image(doc[0], dpi=72)
        large = render_page_image(doc[0], dpi=150)
    finally:
        doc.close()

    check(len(large) > len(small), "150 dpi should produce more data than 72 dpi")


# --------------------------------------------------------------------------- #
# 2. page rebuild: the translated text must come back out
# --------------------------------------------------------------------------- #


def test_rebuild_scanned_page_text_is_extractable() -> None:
    """The translated text is written to the page and can be extracted again."""
    doc = pymupdf.open(_scanned_fixture())
    try:
        text = (
            "[fr] Premier paragraphe de la page traduite, assez long pour "
            "necessiter un retour a la ligne automatique.\n\n"
            "[fr] Deuxieme paragraphe.\n\n"
            "[fr] Troisieme paragraphe final."
        )
        written = rebuild_scanned_page(doc, 0, text, "fr")
        extracted = doc[0].get_text()
    finally:
        doc.close()

    check(written, "rebuild_scanned_page should report success")
    for marker in ("Premier paragraphe", "Deuxieme paragraphe", "Troisieme paragraphe"):
        check(marker in extracted, f"missing from the rebuilt page: {marker!r}")


def test_rebuild_scanned_page_replaces_the_image() -> None:
    """The scanned image is dropped: the page becomes text, not an overlay."""
    doc = pymupdf.open(_scanned_fixture())
    try:
        images_before = len(doc[0].get_images())
        rebuild_scanned_page(doc, 0, "[fr] Texte traduit.", "fr")
        images_after = len(doc[0].get_images())
        page_count = doc.page_count
    finally:
        doc.close()

    check(images_before > 0, "the fixture should start with a scanned image")
    check(images_after == 0, f"the scanned image should be gone, found {images_after}")
    check(page_count == 1, f"page count should be unchanged, got {page_count}")


def test_rebuild_scanned_page_empty_text_returns_false() -> None:
    """Empty or whitespace-only text is refused, leaving the page untouched."""
    doc = pymupdf.open(_scanned_fixture())
    try:
        images_before = len(doc[0].get_images())
        result = rebuild_scanned_page(doc, 0, "   \n\n   \n", "fr")
        images_after = len(doc[0].get_images())
    finally:
        doc.close()

    check(result is False, "empty text should return False")
    check(
        images_after == images_before,
        "a refused rebuild must not modify the original page",
    )


def test_rebuild_scanned_page_overflow_keeps_everything() -> None:
    """Text longer than one page continues onto new pages instead of being cut."""
    doc = pymupdf.open(_scanned_fixture())
    try:
        paragraphs = [f"[fr] Paragraphe numero {index} " + ("mot " * 60) for index in range(30)]
        written = rebuild_scanned_page(doc, 0, "\n\n".join(paragraphs), "fr")
        page_count = doc.page_count
        full_text = "".join(doc[index].get_text() for index in range(page_count))
    finally:
        doc.close()

    check(written, "a long translation should still be written")
    check(page_count > 1, f"overflow should add pages, still {page_count}")
    lost = [index for index in range(30) if f"Paragraphe numero {index}" not in full_text]
    check(not lost, f"paragraphs lost on overflow: {lost}")


def test_rebuild_scanned_page_renders_cjk() -> None:
    """A CJK translation is written with a font that can encode it."""
    text = "第一条 本合同自双方签署之日起生效，双方应当严格遵守本合同的各项约定。"

    doc = pymupdf.open(_scanned_fixture())
    try:
        written = rebuild_scanned_page(doc, 0, text, "zh")
        extracted = doc[0].get_text()
    finally:
        doc.close()

    check(written, "a CJK page should be written")
    check("第一条" in extracted, "CJK characters should survive the rebuild")
    check(
        "?" not in extracted,
        "CJK text should not fall back to '?' placeholders",
    )


def test_rebuild_scanned_page_targets_the_right_page() -> None:
    """Only the requested page is replaced in a multi-page document."""
    doc = pymupdf.open()
    try:
        for index in range(3):
            page = doc.new_page()
            page.insert_text((72, 100), f"ORIGINAL PAGE {index}", fontsize=14)

        rebuild_scanned_page(doc, 1, "[fr] Page du milieu traduite.", "fr")

        first = doc[0].get_text()
        middle = doc[1].get_text()
        last = doc[2].get_text()
        page_count = doc.page_count
    finally:
        doc.close()

    check(page_count == 3, f"page count should stay 3, got {page_count}")
    check("ORIGINAL PAGE 0" in first, "page 0 must not be touched")
    check("Page du milieu traduite" in middle, "page 1 should carry the translation")
    check("ORIGINAL PAGE 2" in last, "page 2 must not be touched")


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()
    make_fixtures.main([])

    tests = [
        test_render_page_image_produces_valid_png,
        test_render_page_image_respects_dpi,
        test_rebuild_scanned_page_text_is_extractable,
        test_rebuild_scanned_page_replaces_the_image,
        test_rebuild_scanned_page_empty_text_returns_false,
        test_rebuild_scanned_page_overflow_keeps_everything,
        test_rebuild_scanned_page_renders_cjk,
        test_rebuild_scanned_page_targets_the_right_page,
    ]

    for test in tests:
        failures_before = len(FAILURES)
        try:
            test()
        except Exception as error:  # noqa: BLE001 - report and continue
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
        status = "OK" if len(FAILURES) == failures_before else "FAIL"
        print(f"[{status}] {test.__name__}")

    print(f"\n{len(tests)} tests, {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
