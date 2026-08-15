"""Font selection per script and glyph-loss detection.

PyMuPDF's built-in fonts cover CJK without an external font file, but no single
one covers every script: writing Korean with a Chinese font silently DELETES the
characters. ``Font.has_glyph`` cannot be trusted here (it reports success for
scripts the font then drops), so loss is detected by writing the text and reading
it back. Mixed-script text is split into runs, each written with its own font.

Portability (audited): the built-in names ("china-s", "japan", "korea") resolve
to Droid Sans Fallback embedded in the pymupdf wheel's own binary — the OS font
directory is never consulted. No font needs to be installed on the machine, and
no font file needs to be shipped in this repo. ``select_font`` still accepts an
explicit ``font_path`` for callers who want to override with their own file.
"""

from __future__ import annotations

import functools

import pymupdf

# Script tags used internally.
KO = "ko"
JA = "ja"
HAN = "han"
LATIN = "latin"
COMMON = "common"  # digits, spaces, punctuation: inherit the neighbouring script

# Built-in font per script. No external file needed.
SCRIPT_FONTS: dict[str, str] = {
    KO: "korea",
    JA: "japan",
    HAN: "china-s",
    LATIN: "helv",
}

# Base-14 latin variants by style.
BASE14_STYLES: dict[tuple[bool, bool], str] = {
    (False, False): "helv",
    (True, False): "hebo",
    (False, True): "heit",
    (True, True): "hebi",
}

# Target language -> preferred script for whole-block font choice.
LANG_SCRIPTS: dict[str, str] = {
    "fr": LATIN,
    "en": LATIN,
    "zh": HAN,
    "zh-tw": HAN,
    "ja": JA,
    "ko": KO,
}

# Typographic characters the Base-14 latin fonts cannot encode (Latin-1 only).
_LATIN1_SUBSTITUTIONS = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "–": "-",
    "—": "-",
    "‑": "-",
    "−": "-",
    "…": "...",
    "•": "-",
    " ": " ",
    " ": " ",
    " ": " ",
    "€": "EUR",
    "Œ": "OE",
    "œ": "oe",
    "™": "(TM)",
    "­": "",
}


def script_of(char: str) -> str:
    """Return the script tag of a single character."""
    code = ord(char)
    if 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
        return KO
    if 0x3040 <= code <= 0x30FF:  # hiragana + katakana
        return JA
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF:
        return HAN
    if code < 0x0250:  # latin + latin-1 supplement + extended-A
        return LATIN
    return COMMON


def split_by_script(text: str) -> list[tuple[str, str]]:
    """Split text into (script, run) pairs; neutral characters join their neighbour."""
    if not text:
        return []

    runs: list[tuple[str, str]] = []
    current: str | None = None
    buffer = ""

    for char in text:
        script = script_of(char)
        if script == COMMON:
            script = current or LATIN
        if current is not None and script != current and buffer:
            runs.append((current, buffer))
            buffer = ""
        current = script
        buffer += char

    if buffer and current is not None:
        runs.append((current, buffer))
    return runs


def dominant_script(text: str) -> str:
    """Return the script covering most characters of the text."""
    counts: dict[str, int] = {}
    for char in text:
        script = script_of(char)
        if script == COMMON or not char.strip():
            continue
        counts[script] = counts.get(script, 0) + 1
    if not counts:
        return LATIN
    return max(counts, key=lambda key: counts[key])


def font_for_script(script: str, *, bold: bool = False, italic: bool = False) -> str:
    """Return the built-in font name for a script, honouring style for latin."""
    if script == LATIN:
        return BASE14_STYLES[(bold, italic)]
    return SCRIPT_FONTS.get(script, "helv")


def select_font(
    target_lang: str,
    *,
    bold: bool = False,
    italic: bool = False,
    font_path: str | None = None,
) -> tuple[str, str | None]:
    """Return (fontname, fontfile) for a target language."""
    if font_path:
        return "custom", font_path
    script = LANG_SCRIPTS.get(target_lang.lower(), LATIN)
    return font_for_script(script, bold=bold, italic=italic), None


def sanitize_latin1(text: str) -> tuple[str, int]:
    """Replace characters the Base-14 latin fonts cannot encode.

    Returns the cleaned text and the number of characters replaced by "?".
    """
    for source, replacement in _LATIN1_SUBSTITUTIONS.items():
        text = text.replace(source, replacement)

    unsupported = 0
    chars: list[str] = []
    for char in text:
        if ord(char) > 255:
            chars.append("?")
            unsupported += 1
        else:
            chars.append(char)
    return "".join(chars), unsupported


@functools.lru_cache(maxsize=4096)
def _renderable_chars(charset: frozenset[str], fontname: str, fontfile: str | None) -> frozenset[str]:
    """Return the subset of characters the font actually renders.

    Ground truth: write them to a scratch PDF and read them back. ``has_glyph``
    is unreliable for the built-in CJK fonts.
    """
    probe = "".join(sorted(charset))
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_textbox(
            pymupdf.Rect(10, 10, page.rect.width - 10, page.rect.height - 10),
            probe,
            fontname=fontname,
            fontfile=fontfile,
            fontsize=8,
        )
        written = doc.tobytes()
    finally:
        doc.close()

    reread = pymupdf.open("pdf", written)
    try:
        rendered = reread[0].get_text()
    finally:
        reread.close()
    return frozenset(char for char in charset if char in rendered)


def missing_chars(text: str, fontname: str, fontfile: str | None = None) -> list[str]:
    """Return characters of the text the font would silently drop."""
    charset = frozenset(char for char in text if char.strip())
    if not charset:
        return []
    renderable = _renderable_chars(charset, fontname, fontfile)
    return [char for char in charset if char not in renderable]


# Built-in fonts able to render latin text beyond Latin-1 (typographic quotes,
# the euro sign, the oe ligature). Used to avoid degrading French typography.
_LATIN_FALLBACKS = ("japan", "china-s", "korea")


def best_font_for(text: str, fontname: str, fontfile: str | None = None) -> tuple[str, str | None]:
    """Return a font rendering the text fully, preferring the requested one.

    The Base-14 latin fonts are Latin-1 only and drop ’ € … œ. When the chosen
    font cannot render everything, a built-in CJK font that covers the full
    latin range is used instead rather than degrading the text.
    """
    if fontfile is not None:
        return fontname, fontfile
    if not missing_chars(text, fontname):
        return fontname, None
    for candidate in _LATIN_FALLBACKS:
        if not missing_chars(text, candidate):
            return candidate, None
    return fontname, None
