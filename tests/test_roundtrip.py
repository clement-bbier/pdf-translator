"""End-to-end test: extract, replace every block with longer text, rebuild.

No AI and no network. Run: python tests/test_roundtrip.py
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
from app.extractor import extract  # noqa: E402
from app.models import DocumentData  # noqa: E402
from app.rebuilder import rebuild  # noqa: E402
from app.translator import MockTranslator  # noqa: E402
from tests import make_fixtures  # noqa: E402

OUTPUT_DIR = ROOT / "output"

# Characters that must survive a rebuild untouched.
SPECIAL_CHARS = "’€…œçû"

# A block whose extracted color has at least this much red, and not much green/blue,
# is considered "red" for the color round-trip check.
_RED_MIN = 0.4
_OTHER_MAX = 0.3

# Latin filler used to lengthen latin blocks.
_LOREM = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua "
)
# CJK filler, so a CJK block stays CJK after lengthening.
_LOREM_CJK = "合同条款约定双方权利义务履行期限违约责任争议解决"

# A block may not lose more than this share of its content to truncation.
MAX_TRUNCATION_RATE = 0.20

# Share of pixels allowed to turn white inside an image area. Redaction with the
# wrong settings punches white rectangles through images; counting image objects
# does not catch it, so the pixels are compared instead.
MAX_WHITENED_RATIO = 0.02


def _whitened_ratio(before: pymupdf.Page, after: pymupdf.Page) -> float:
    """Return the share of image pixels erased to white by the rebuild.

    Only areas covered by an image are compared, and only pixels that were
    coloured before and are white after. Redaction with the wrong settings
    punches white rectangles through images; counting image objects does not
    catch it, so the pixels themselves are compared.

    Writing replacement text darkens pixels, it never whitens them, so any
    coloured pixel turning white inside an image means the background was
    erased. Measured on the stress fixture: 0.00% with the correct redaction
    settings, 9.70% with PyMuPDF's destructive defaults.
    """
    boxes = [pymupdf.Rect(info["bbox"]) for info in before.get_image_info()]
    if not boxes:
        return 0.0

    dpi = 50
    scale = dpi / 72.0
    old = before.get_pixmap(dpi=dpi)
    new = after.get_pixmap(dpi=dpi)
    if (old.width, old.height, old.n) != (new.width, new.height, new.n):
        return 0.0

    coloured = 0
    whitened = 0
    for box in boxes:
        x0 = max(int(box.x0 * scale), 0)
        y0 = max(int(box.y0 * scale), 0)
        x1 = min(int(box.x1 * scale), old.width)
        y1 = min(int(box.y1 * scale), old.height)
        for y in range(y0, y1):
            row = y * old.width
            for x in range(x0, x1):
                offset = (row + x) * old.n
                if min(old.samples[offset : offset + 3]) >= 250:
                    continue
                coloured += 1
                if min(new.samples[offset : offset + 3]) >= 250:
                    whitened += 1
    return whitened / coloured if coloured else 0.0


def _grow(text: str, factor: float = 1.5) -> str:
    """Return text expanded to roughly `factor` times its length."""
    target = int(len(text) * factor)
    filler = _LOREM_CJK if fonts.dominant_script(text) != fonts.LATIN else _LOREM
    grown = text
    while len(grown) < target:
        grown += filler[: target - len(grown)]
    return grown


def _build_translations(document: DocumentData) -> dict[str, str]:
    """Grow every block; inject the special characters into the first latin one."""
    translations = {block.id: _grow(block.text) for block in document.all_blocks()}

    for block in document.all_blocks():
        if fonts.dominant_script(block.text) == fonts.LATIN and len(block.text) > 40:
            translations[block.id] = f"{SPECIAL_CHARS} {_grow(block.text)}"
            break
    return translations


def _check_red_survives(result: pymupdf.Document, red_blocks, translations: dict[str, str]) -> list[str]:
    """Verify the translated text of each red block is still written in red."""
    errors: list[str] = []
    for block in red_blocks:
        translated = translations.get(block.id, "").strip()
        if not translated:
            continue
        page = result[int(block.id.split("_")[0][1:])]
        content = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
        found_red = False
        for text_block in content["blocks"]:
            if text_block["type"] != 0:
                continue
            for line in text_block["lines"]:
                for span in line["spans"]:
                    if span["text"].strip() and span["text"] in translated:
                        color = int(span["color"])
                        red = ((color >> 16) & 0xFF) / 255.0
                        green = ((color >> 8) & 0xFF) / 255.0
                        blue = (color & 0xFF) / 255.0
                        if red >= _RED_MIN and green <= _OTHER_MAX and blue <= _OTHER_MAX:
                            found_red = True
        if not found_red:
            errors.append(f"block {block.id} lost its red color after rebuild")
    return errors


def _check(fixture: Path) -> list[str]:
    """Run one fixture through the pipeline; return the list of failures."""
    errors: list[str] = []
    source = pymupdf.open(fixture)
    source_pages = source.page_count
    source_images = [len(source[i].get_images()) for i in range(source_pages)]
    source_drawings = [len(source[i].get_drawings()) for i in range(source_pages)]
    has_images = any(source_images)

    document = extract(fixture)

    # A page carrying only a full-page image must be flagged, not silently
    # treated as an empty page: later phases route those to a different path.
    expect_scanned = fixture.stem == "scanned"
    if expect_scanned != any(page.is_scanned for page in document.pages):
        errors.append(f"is_scanned should be {expect_scanned}")

    translations = _build_translations(document)
    out_path = OUTPUT_DIR / f"{fixture.stem}_out.pdf"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    red_blocks = [
        block
        for block in document.all_blocks()
        if ((block.color >> 16) & 0xFF) / 255.0 >= _RED_MIN
        and ((block.color >> 8) & 0xFF) / 255.0 <= _OTHER_MAX
        and (block.color & 0xFF) / 255.0 <= _OTHER_MAX
    ]
    if fixture.stem == "simple" and not red_blocks:
        errors.append("expected a red block in simple.pdf, found none")

    stats = rebuild(fixture, document, translations, out_path)

    if not out_path.is_file():
        return [f"output not created: {out_path.name}"]

    result = pymupdf.open(out_path)
    try:
        if result.page_count != source_pages:
            errors.append(f"page count {source_pages} -> {result.page_count}")

        for index in range(min(source_pages, result.page_count)):
            page = result[index]
            page_data = document.pages[index]

            if not page_data.is_scanned and page_data.blocks and not page.get_text().strip():
                errors.append(f"page {index + 1} lost all its text")

            images = len(page.get_images())
            if images < source_images[index]:
                errors.append(
                    f"page {index + 1} lost an image ({source_images[index]} -> {images})"
                )

            drawings = len(page.get_drawings())
            if drawings < source_drawings[index]:
                errors.append(
                    f"page {index + 1} lost line art ({source_drawings[index]} -> {drawings})"
                )

            # Only meaningful where an image can actually be damaged.
            if has_images:
                ratio = _whitened_ratio(source[index], page)
                if ratio > MAX_WHITENED_RATIO:
                    errors.append(
                        f"page {index + 1} background erased ({ratio:.1%} of pixels whitened)"
                    )

        if red_blocks:
            errors.extend(_check_red_survives(result, red_blocks, translations))

        text = "".join(result[i].get_text() for i in range(result.page_count))

        # The control block must keep its special characters.
        if any(char in value for value in translations.values() for char in SPECIAL_CHARS):
            lost = [char for char in SPECIAL_CHARS if char not in text]
            if lost:
                errors.append(f"special characters lost: {''.join(lost)}")

        # A CJK source must stay CJK.
        source_cjk = any(
            fonts.script_of(char) in (fonts.HAN, fonts.JA, fonts.KO)
            for block in document.all_blocks()
            for char in block.text
        )
        if source_cjk:
            output_cjk = sum(
                1 for char in text if fonts.script_of(char) in (fonts.HAN, fonts.JA, fonts.KO)
            )
            if output_cjk == 0:
                errors.append("CJK characters disappeared from the output")
    finally:
        result.close()
        source.close()

    if stats.blocks_failed:
        errors.append(f"{stats.blocks_failed} blocks could not be written")
    if stats.blocks_written != stats.blocks_total:
        errors.append(f"written {stats.blocks_written}/{stats.blocks_total}")
    if stats.glyphs_missing:
        errors.append(f"{stats.glyphs_missing} characters could not be rendered")
    if stats.blocks_total:
        rate = stats.blocks_truncated / stats.blocks_total
        if rate > MAX_TRUNCATION_RATE:
            errors.append(f"truncation rate {rate:.0%} above {MAX_TRUNCATION_RATE:.0%}")

    print(f"       {stats.summary()}")
    return errors


# --------------------------------------------------------------------------- #
# Regression: the font-shrink/truncate fallback must never draw a rejected
# candidate. A prior bug drew every probed size/length directly on the page via
# insert_textbox, so when a block did not fit at its original size, failed
# attempts stayed visible underneath the final one, stacking growing fragments
# of the same logical block on top of each other.
# --------------------------------------------------------------------------- #


def _check_no_stacked_fragments(fixture: Path) -> list[str]:
    """Rebuild `fixture` with MockTranslator (which grows text ~1.4x) and check
    that no block's translated text appears more than once in its own area.

    A word run repeated 8+ characters is a strong signal of a stacked draw: the
    shrink/truncate fallback overlapping its own earlier, shorter attempts.
    """
    errors: list[str] = []
    document = extract(fixture)
    mock = MockTranslator()

    translatable = [block for block in document.all_blocks() if block.is_translatable]
    translations = {
        block.id: mock.translate_blocks([block.text], "en", "fr")[0] for block in translatable
    }

    out_path = OUTPUT_DIR / f"{fixture.stem}_stack_check.pdf"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rebuild(fixture, document, translations, out_path)

    result = pymupdf.open(out_path)
    try:
        for block in translatable:
            translated = translations[block.id].strip()
            if not translated:
                continue
            page = result[int(block.id.split("_")[0][1:])]
            # Search a generous area around the original bbox: a shrunk block
            # can end up smaller than its box, but a stacked artifact from this
            # bug always lands inside the same box as the final draw.
            rect = pymupdf.Rect(block.bbox) + (-2, -2, 2, 20)
            area_text = page.get_textbox(rect)

            # A stacked draw repeats a leading run of the translated text. Probe
            # a fixed-length prefix (long enough to be specific, short enough to
            # survive truncation) for more than one occurrence in the block's area.
            probe_len = min(12, len(translated))
            probe = translated[:probe_len]
            if probe and area_text.count(probe) > 1:
                errors.append(
                    f"{fixture.name} block {block.id}: '{probe}' appears "
                    f"{area_text.count(probe)} times in its own area (stacked fragments)"
                )
    finally:
        result.close()

    out_path.unlink(missing_ok=True)
    return errors


def test_no_stacked_fragments_on_overflow() -> list[str]:
    """Run the stacked-fragment regression check on the dense, tight-bbox fixture."""
    return _check_no_stacked_fragments(make_fixtures.FIXTURES_DIR / "dense_layout.pdf")


# --------------------------------------------------------------------------- #
# CJK portability: the write/read-back roundtrip must lose zero characters for
# every supported CJK language, using only the fonts embedded in the pymupdf
# wheel (audited: "china-s"/"japan"/"korea" all resolve to Droid Sans Fallback
# shipped inside mupdfcpp64.dll — no OS-installed font is ever consulted).
# --------------------------------------------------------------------------- #

_CJK_SAMPLES = {
    "zh": "第一条 本合同自双方签署之日起生效，双方应当严格遵守约定。",
    "zh-tw": "第一條 本合約自雙方簽署之日起生效，雙方應當嚴格遵守約定。",
    "ja": "だいいちじょう ほんけいやくは、りょうとうじしゃの署名の日から効力を生じる。",
    "ko": "제1조 본 계약은 양 당사자가 서명한 날부터 효력이 발생한다.",
}


def test_cjk_roundtrip_zero_loss() -> list[str]:
    """Every CJK language survives the write/read-back probe with 0 lost chars."""
    errors: list[str] = []
    for lang, sample in _CJK_SAMPLES.items():
        for script, run in fonts.split_by_script(sample):
            fontname = fonts.font_for_script(script)
            lost = fonts.missing_chars(run, fontname)
            if lost:
                errors.append(
                    f"{lang}: {len(lost)} character(s) lost with font {fontname}: "
                    f"{''.join(lost)}"
                )
    return errors


# --------------------------------------------------------------------------- #
# Regression: a block pymupdf cannot fit at all (not even truncated) must
# never vanish without a trace. A version change (pymupdf 1.28.2 -> 1.27.2.3)
# altered textbox-fitting behaviour on this fixture's tightest block, p0_b6
# ("Ready for internal rollout."): the block is redacted (erased) before the
# replacement is drawn, so a "could not fit" that stays silent means the
# translated content — and the original — both disappear from the document
# with nothing in JobResult.warnings to say so. This test locks in: either
# the block's text survives somewhere in its own page (in full, truncated,
# or as source), or rebuild() reports it via RebuildStats.blocks_failed.
# --------------------------------------------------------------------------- #


def test_unfittable_block_never_silently_dropped() -> list[str]:
    """A block too large to fit by any means must be text-present or reported failed."""
    errors: list[str] = []
    fixture = make_fixtures.FIXTURES_DIR / "dense_layout.pdf"
    document = extract(fixture)

    target = next((block for block in document.all_blocks() if block.id == "p0_b6"), None)
    if target is None:
        return ["fixture block p0_b6 not found (dense_layout.pdf layout changed?)"]

    # 5x growth with long unbroken words (no short repeats to fall back on)
    # is what actually defeats the truncate-to-"..." fallback on this block's
    # tight box, confirmed by reproduction: growing the block's own text via
    # repetition always finds a fitting truncation, this filler does not.
    oversized = _grow(target.text, factor=5.0)
    translations = {target.id: oversized}

    out_path = OUTPUT_DIR / "dense_layout_unfittable_check.pdf"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stats = rebuild(fixture, document, translations, out_path)

    result = pymupdf.open(out_path)
    try:
        page = result[0]
        rect = pymupdf.Rect(target.bbox) + (-5, -5, 5, 20)
        area_text = page.get_textbox(rect).strip()
        page_text = page.get_text()

        text_present = bool(area_text) or target.text in page_text
        reported_failed = stats.blocks_failed > 0

        if not text_present and not reported_failed:
            errors.append(
                "block p0_b6 vanished with no text in its area and no "
                "RebuildStats.blocks_failed signal — silent content loss"
            )
    finally:
        result.close()

    out_path.unlink(missing_ok=True)
    return errors


# dense_layout.pdf is deliberately built with boxes too tight to hold a 1.5x
# growth even after truncation on some blocks — that is the point of the
# fixture (it forces the overflow fallback path). It is checked separately by
# test_no_stacked_fragments_on_overflow, not by the generic _check, which
# asserts every block survives growth without failure or truncation-rate limits
# that this fixture is designed to hit.
_GENERIC_CHECK_EXCLUDED = {"dense_layout.pdf"}


def main() -> int:
    """Run the roundtrip on every fixture."""
    app_config.force_utf8_stdout()
    make_fixtures.main([])

    failures = 0
    checked = 0
    for name in make_fixtures.FIXTURES:
        if name in _GENERIC_CHECK_EXCLUDED:
            continue
        checked += 1
        fixture = make_fixtures.FIXTURES_DIR / name
        if not fixture.is_file():
            print(f"[FAIL] {name}: fixture missing")
            failures += 1
            continue

        errors = _check(fixture)
        if errors:
            failures += 1
            print(f"[FAIL] {name}")
            for error in errors:
                print(f"       - {error}")
        else:
            print(f"[OK]   {name}")

    total = checked

    stacked_errors = test_no_stacked_fragments_on_overflow()
    total += 1
    if stacked_errors:
        failures += 1
        print("[FAIL] test_no_stacked_fragments_on_overflow")
        for error in stacked_errors:
            print(f"       - {error}")
    else:
        print("[OK]   test_no_stacked_fragments_on_overflow")

    unfittable_errors = test_unfittable_block_never_silently_dropped()
    total += 1
    if unfittable_errors:
        failures += 1
        print("[FAIL] test_unfittable_block_never_silently_dropped")
        for error in unfittable_errors:
            print(f"       - {error}")
    else:
        print("[OK]   test_unfittable_block_never_silently_dropped")

    cjk_errors = test_cjk_roundtrip_zero_loss()
    total += 1
    if cjk_errors:
        failures += 1
        print("[FAIL] test_cjk_roundtrip_zero_loss")
        for error in cjk_errors:
            print(f"       - {error}")
    else:
        print("[OK]   test_cjk_roundtrip_zero_loss")

    print(f"\n{total} tests, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
