"""Numbers check: every number and currency of the source must survive in the translation.

Pure functions, standard library only. Comparison uses absolute values (a
translation may carry a sign in words), accepts every reading of an ambiguous
separator or of a scaled amount (7,518億 is 7518 or 751.8 billion), and requires
the percent kind to match. The choices lean towards few false positives, since
each one costs a retry and a human review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.verification.dates import find_dates, normalize_keep_offsets
from app.verification.findings import Finding


@dataclass(frozen=True)
class NumberMention:
    """A number found in a text: every acceptable value, percent flag, raw token."""

    values: frozenset[Decimal]
    is_percent: bool
    raw: str


_MULTIPLIERS = {
    "百万": Decimal(10**6),
    "百萬": Decimal(10**6),
    "千万": Decimal(10**7),
    "千萬": Decimal(10**7),
    "万": Decimal(10**4),
    "萬": Decimal(10**4),
    "亿": Decimal(10**8),
    "億": Decimal(10**8),
    "千": Decimal(10**3),
    "thousand": Decimal(10**3),
    "mille": Decimal(10**3),
    "million": Decimal(10**6),
    "millions": Decimal(10**6),
    "mn": Decimal(10**6),
    "billion": Decimal(10**9),
    "milliard": Decimal(10**9),
    "milliards": Decimal(10**9),
    "bn": Decimal(10**9),
    "md": Decimal(10**9),
    "mds": Decimal(10**9),
    "trillion": Decimal(10**12),
}

_MULTIPLIER_RE = re.compile(
    r"[ \t]*(百万|百萬|千万|千萬|万|萬|亿|億|千"
    r"|thousand|trillion|billion|milliards?|millions?|mille|mds?|bn|mn)(?![A-Za-z])",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(
    r"[ \t]*(?:%|(?:per[ \t]?cent|percent|pour[ \t]?cent|percentage[ \t]+points?"
    r"|points?[ \t]+de[ \t]+pourcentage)(?![A-Za-z])|个百分点|個百分點|百分点|百分點)",
    re.IGNORECASE,
)
_RANGE_GAP_RE = re.compile(r"\s*(?:[-–—~]|至|到|to|à)\s*", re.IGNORECASE)

_TOKEN_DEFAULT_RE = re.compile(r"(?<!\d)\d+(?:[,.]\d+)*")
_TOKEN_FR_RE = re.compile(r"(?<!\d)(?:\d{1,3}(?: \d{3}(?!\d))+(?:[.,]\d+)*|\d+(?:[.,]\d+)*)")

_CURRENCY_CODES = {"RMB", "CNY", "HKD", "USD", "EUR", "JPY", "GBP"}

_CURRENCY_PATTERNS = {
    "HKD": [
        r"HK\$",
        r"\bHKD\b",
        "港元",
        "港幣",
        "港币",
        r"\bHong Kong dollars?\b",
        r"\bdollars? de Hong Kong\b",
        r"\bdollars? HK\b",
    ],
    "RMB": [
        r"\bRMB\b",
        r"\bCNY\b",
        "人民幣",
        "人民币",
        r"\brenminbi\b",
        r"\byuans?\b",
    ],
    "USD": [
        r"US\$",
        r"\bUSD\b",
        "美元",
        r"\bUS dollars?\b",
        r"\bdollars? am[ée]ricains?\b",
    ],
}


def _word_edges(pattern: str) -> str:
    """Letter-based word edges: digits may touch a code (RMB751.8, HK$14)."""
    if pattern.startswith(r"\b"):
        pattern = "(?<![A-Za-z])" + pattern[2:]
    if pattern.endswith(r"\b"):
        pattern = pattern[:-2] + "(?![A-Za-z])"
    return pattern


_CURRENCY_RES = {
    code: re.compile("|".join(_word_edges(p) for p in patterns), re.IGNORECASE)
    for code, patterns in _CURRENCY_PATTERNS.items()
}


def find_currencies(text: str) -> set[str]:
    """Canonical currency codes (HKD, RMB, USD) mentioned in a text."""
    return {code for code, rx in _CURRENCY_RES.items() if rx.search(text)}


def _dec(text: str) -> Decimal:
    return Decimal(text)


def _readings(token: str, lang: str) -> list[set[Decimal]]:
    """One set of acceptable values per number the token stands for."""
    t = token.replace(" ", "")
    has_dot, has_comma = "." in t, "," in t
    if not has_dot and not has_comma:
        return [{_dec(t)}]
    if has_dot and has_comma:
        last = max(t.rfind("."), t.rfind(","))
        integer = t[:last].replace(".", "").replace(",", "")
        return [{_dec(f"{integer}.{t[last + 1 :]}")}]
    sep = "." if has_dot else ","
    parts = t.split(sep)
    groups_ok = (
        1 <= len(parts[0]) <= 3
        and not parts[0].startswith("0")
        and all(len(p) == 3 for p in parts[1:])
    )
    native_thousands = (sep == ",") != (lang == "fr")
    if len(parts) > 2:
        if groups_ok and (native_thousands or sep == ","):
            return [{_dec("".join(parts))}]
        return [{_dec(p)} for p in parts]
    first, second = parts
    if groups_ok:
        thousands = _dec(first + second)
        if native_thousands and sep == ",":
            return [{thousands}]
        return [{thousands, _dec(f"{first}.{second}")}]
    decimal = _dec(f"{first}.{second}")
    if sep == "," and lang != "fr":
        return [{decimal, _dec(first), _dec(second)}]
    return [{decimal}]


@dataclass
class _Token:
    start: int  # core start (without sign)
    end: int
    raw_start: int
    readings: list[set[Decimal]]
    percent: bool


def _scan(norm: str, lang: str, skip_identifiers: bool) -> list[_Token]:
    spans = [(d.start, d.end) for d in find_dates(norm)]
    pattern = _TOKEN_FR_RE if lang == "fr" else _TOKEN_DEFAULT_RE
    tokens: list[_Token] = []
    for m in pattern.finditer(norm):
        start, end = m.start(), m.end()
        if any(s <= start < e for s, e in spans):
            continue
        if end < len(norm) and norm[end] in "月日월일":
            continue
        run_start = start
        while run_start > 0 and norm[run_start - 1].isascii() and norm[run_start - 1].isalpha():
            run_start -= 1
        if (
            skip_identifiers
            and run_start < start
            and norm[run_start:start].upper() not in _CURRENCY_CODES
        ):
            continue
        readings = _readings(m.group(), lang)
        raw_start = start
        if start > 0 and norm[start - 1] in "-−–":
            before = norm[start - 2] if start > 1 else " "
            if not before.isalnum():
                raw_start = start - 1
        percent = False
        if len(readings) == 1:
            mult = _MULTIPLIER_RE.match(norm, end)
            if mult:
                factor = _MULTIPLIERS[mult.group(1).lower()]
                readings = [readings[0] | {v * factor for v in readings[0]}]
                end = mult.end()
            pct = _PERCENT_RE.match(norm, end)
            if pct:
                percent = True
                end = pct.end()
        tokens.append(_Token(start, end, raw_start, readings, percent))
    return tokens


def extract_numbers(text: str, lang: str, *, skip_identifiers: bool = True) -> list[NumberMention]:
    """Numbers of a text with every acceptable value.

    Digits inside dates and inside identifiers such as ``A4`` or ``COVID19``
    are ignored (``skip_identifiers=False`` keeps the identifiers, used for the
    translation so that ``Q3`` can still match a source ``3``).
    """
    norm = normalize_keep_offsets(text)
    tokens = _scan(norm, lang, skip_identifiers)
    # A lower bound inherits the percent kind of its range: 10-15%, 10 to 15 per cent.
    for i in range(len(tokens) - 2, -1, -1):
        current, following = tokens[i], tokens[i + 1]
        if (
            not current.percent
            and following.percent
            and len(current.readings) == 1
            and _RANGE_GAP_RE.fullmatch(norm[current.end : following.start])
        ):
            current.percent = True
    mentions: list[NumberMention] = []
    for token in tokens:
        raw = text[token.raw_start : token.end]
        for values in token.readings:
            mentions.append(
                NumberMention(
                    frozenset(values),
                    token.percent,
                    raw if len(token.readings) == 1 else str(next(iter(values))),
                )
            )
    return mentions


def check_numbers(key: str, source: str, translation: str, src: str, tgt: str) -> list[Finding]:
    """Findings for source numbers or currencies missing from the translation."""
    findings: list[Finding] = []
    translated = extract_numbers(translation, tgt, skip_identifiers=False)
    seen: set[str] = set()
    for mention in extract_numbers(source, src):
        if mention.raw in seen:
            continue
        seen.add(mention.raw)
        found = any(
            mention.values & other.values and (other.is_percent or not mention.is_percent)
            for other in translated
        )
        if not found:
            findings.append(
                Finding("numbers", key, f"number not found in the translation: {mention.raw}")
            )
    kept = find_currencies(translation)
    for code in sorted(find_currencies(source)):
        if code not in kept:
            findings.append(Finding("numbers", key, f"currency not kept: {code}"))
    return findings
