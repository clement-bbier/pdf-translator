"""Dates check: every date of the source must survive in the translation.

Pure functions, standard library only. The recognisers are language-agnostic
(CJK, ISO, DD/MM/YYYY, English and French month names); the same
``find_dates`` is used by ``numbers.py`` to ignore the digits of dates.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from dataclasses import dataclass

from app.verification.findings import Finding


@dataclass(frozen=True)
class DateMention:
    """A date found in a text. ``start``/``end`` are offsets in the original text."""

    year: int
    month: int
    day: int | None
    start: int
    end: int
    # Other valid (year, month, day) readings when DD/MM and MM/DD are both possible.
    alternatives: tuple[tuple[int, int, int], ...] = ()


def normalize_keep_offsets(text: str) -> str:
    """NFKC per character (full-width digits and punctuation), offsets preserved."""
    out: list[str] = []
    for char in text:
        normalized = unicodedata.normalize("NFKC", char)
        out.append(normalized if len(normalized) == 1 else char)
    return "".join(out)


_MONTHS = {
    "january": 1,
    "jan": 1,
    "janvier": 1,
    "february": 2,
    "feb": 2,
    "fevrier": 2,
    "fev": 2,
    "march": 3,
    "mar": 3,
    "mars": 3,
    "april": 4,
    "apr": 4,
    "avril": 4,
    "avr": 4,
    "may": 5,
    "mai": 5,
    "june": 6,
    "jun": 6,
    "juin": 6,
    "july": 7,
    "jul": 7,
    "juillet": 7,
    "august": 8,
    "aug": 8,
    "aout": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "septembre": 9,
    "october": 10,
    "oct": 10,
    "octobre": 10,
    "november": 11,
    "nov": 11,
    "novembre": 11,
    "december": 12,
    "dec": 12,
    "decembre": 12,
}

_CN_DIGITS = {"〇": 0, "○": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
_CN_DIGITS.update({"六": 6, "七": 7, "八": 8, "九": 9})

_CJK_ARABIC = r"(?<!\d)(\d{4})\s*[年년]\s*(\d{1,2})\s*[月월](?:\s*(\d{1,2})\s*[日일])?"
_CJK_NUMERAL = (
    r"([〇○零一二三四五六七八九]{4})\s*年\s*([一二三四五六七八九十]{1,3})\s*月"
    r"(?:\s*([一二三四五六七八九十廿卅]{1,3})\s*日)?"
)
_ISO = r"(?<![\d/.\-])(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)"
_DMY = r"(?<![\d/.\-])(\d{1,2})([/.])(\d{1,2})\2(\d{4})(?![\d/.])"
_NOT_AFTER = r"(?<![0-9A-Za-zÀ-ɏ_])"  # Latin word boundary: CJK text may touch a date
_DAY_FIRST = (
    _NOT_AFTER + r"(?:(\d{1,2})(?:st|nd|rd|th|er)?\s+(?:of\s+|de\s+)?)?"
    r"([A-Za-zÀ-ɏ]{3,9})\.?\s+(\d{4})(?!\d)"
)
_MONTH_FIRST = _NOT_AFTER + r"([A-Za-zÀ-ɏ]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)"

_RE_CJK_ARABIC = re.compile(_CJK_ARABIC)
_RE_CJK_NUMERAL = re.compile(_CJK_NUMERAL)
_RE_ISO = re.compile(_ISO)
_RE_DMY = re.compile(_DMY)
_RE_DAY_FIRST = re.compile(_DAY_FIRST, re.IGNORECASE)
_RE_MONTH_FIRST = re.compile(_MONTH_FIRST, re.IGNORECASE)


def _month_number(word: str) -> int | None:
    stripped = "".join(
        c for c in unicodedata.normalize("NFD", word.lower()) if not unicodedata.combining(c)
    )
    return _MONTHS.get(stripped)


def _valid(year: int, month: int, day: int | None) -> bool:
    try:
        datetime.date(year, month, day or 1)
    except ValueError:
        return False
    return True


def _cn_digit(char: str) -> int | None:
    return _CN_DIGITS.get(char)


def _cn_small(text: str) -> int | None:
    """Chinese numeral 1..31 (十, 十X, X十, X十Y, 廿X, 卅)."""
    if not text:
        return None
    if text[0] in "廿卅":
        base = 20 if text[0] == "廿" else 30
        if len(text) == 1:
            return base
        ones = _cn_digit(text[1]) if len(text) == 2 else None
        return None if ones is None else base + ones
    if "十" in text:
        head, _, tail = text.partition("十")
        if len(head) > 1 or len(tail) > 1:
            return None
        tens = 1 if not head else _cn_digit(head)
        ones = 0 if not tail else _cn_digit(tail)
        return None if tens is None or ones is None else tens * 10 + ones
    return _cn_digit(text) if len(text) == 1 else None


def _cn_year(text: str) -> int | None:
    digits = [_cn_digit(c) for c in text]
    if any(d is None for d in digits):
        return None
    return int("".join(str(d) for d in digits))


def _build(
    year: int,
    month: int,
    day: int | None,
    start: int,
    end: int,
    alternatives: tuple[tuple[int, int, int], ...] = (),
) -> DateMention | None:
    if not _valid(year, month, day):
        return None
    return DateMention(year, month, day, start, end, alternatives)


def find_dates(text: str) -> list[DateMention]:
    """Find the valid dates of a text, ordered by position, without overlaps."""
    norm = normalize_keep_offsets(text)
    found: list[DateMention] = []

    def add(mention: DateMention | None) -> None:
        if mention is not None:
            found.append(mention)

    for m in _RE_CJK_ARABIC.finditer(norm):
        day = int(m.group(3)) if m.group(3) else None
        add(_build(int(m.group(1)), int(m.group(2)), day, m.start(), m.end()))

    for m in _RE_CJK_NUMERAL.finditer(norm):
        year, month = _cn_year(m.group(1)), _cn_small(m.group(2))
        day = _cn_small(m.group(3)) if m.group(3) else None
        if year is not None and month is not None and (day is not None or not m.group(3)):
            add(_build(year, month, day, m.start(), m.end()))

    for m in _RE_ISO.finditer(norm):
        add(_build(int(m.group(1)), int(m.group(2)), int(m.group(3)), m.start(), m.end()))

    for m in _RE_DMY.finditer(norm):
        first, second, year = int(m.group(1)), int(m.group(3)), int(m.group(4))
        readings = [(year, mo, d) for d, mo in ((first, second), (second, first))]
        readings = [r for r in dict.fromkeys(readings) if _valid(*r)]
        if readings:
            y, mo, d = readings[0]
            add(DateMention(y, mo, d, m.start(), m.end(), tuple(readings[1:])))

    for m in _RE_DAY_FIRST.finditer(norm):
        month = _month_number(m.group(2))
        if month is not None:
            day = int(m.group(1)) if m.group(1) else None
            add(_build(int(m.group(3)), month, day, m.start(), m.end()))

    for m in _RE_MONTH_FIRST.finditer(norm):
        month = _month_number(m.group(1))
        if month is not None:
            add(_build(int(m.group(3)), month, int(m.group(2)), m.start(), m.end()))

    found.sort(key=lambda d: (d.start, -(d.end - d.start)))
    kept: list[DateMention] = []
    for mention in found:
        if not kept or mention.start >= kept[-1].end:
            kept.append(mention)
    return kept


def find_cjk_dates(text: str) -> list[str]:
    """Raw CJK date patterns (Arabic or Chinese numerals with 年/月/日 or 년/월/일)."""
    norm = normalize_keep_offsets(text)
    spans = [m.span() for rx in (_RE_CJK_ARABIC, _RE_CJK_NUMERAL) for m in rx.finditer(norm)]
    return [text[start:end] for start, end in sorted(spans)]


def _readings(mention: DateMention) -> set[tuple[int, int, int | None]]:
    return {(mention.year, mention.month, mention.day), *mention.alternatives}


def check_dates(key: str, source: str, translation: str, src: str, tgt: str) -> list[Finding]:
    """Findings for source dates missing from the translation, and CJK dates left as is."""
    findings: list[Finding] = []
    translated = find_dates(translation)
    full = {r for t in translated for r in _readings(t) if r[2] is not None}
    months = {(r[0], r[1]) for t in translated for r in _readings(t)}
    seen: set[str] = set()
    for mention in find_dates(source):
        raw = source[mention.start : mention.end]
        if raw in seen:
            continue
        seen.add(raw)
        readings = _readings(mention)
        if mention.day is None:
            matched = any((r[0], r[1]) in months for r in readings)
        else:
            matched = bool(readings & full)
        if not matched:
            findings.append(Finding("dates", key, f"date not found in the translation: {raw}"))
    if tgt in ("en", "fr"):
        for raw in find_cjk_dates(translation):
            findings.append(Finding("dates", key, f"CJK date left untranslated: {raw}"))
    return findings
