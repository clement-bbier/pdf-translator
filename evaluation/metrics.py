"""Standard-library translation metrics: pure functions, no I/O."""

from __future__ import annotations

import difflib
import math
import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Sequence
from typing import Any, Protocol

_NUMBER_RE = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


class GlossaryEntry(Protocol):
    """Anything with a source term and a required target term per language."""

    source: str

    def target(self, tgt: str) -> str: ...


def normalise(text: str) -> str:
    """NFKC, casefold, collapse whitespace runs, strip."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split())


def similarity(hyp: str, ref: str) -> float:
    """Ratio in [0, 1] of the normalised strings (difflib)."""
    matcher = difflib.SequenceMatcher(None, normalise(hyp), normalise(ref), autojunk=False)
    return matcher.ratio()


def _char_ngrams(text: str, n: int) -> Counter[str]:
    return Counter(text[i : i + n] for i in range(len(text) - n + 1))


def chrf(hyp: str, ref: str, *, max_n: int = 6, beta: float = 2.0) -> float:
    """chrF (Popovic 2015) on a 0-100 scale, character n-grams only."""
    h = "".join(unicodedata.normalize("NFKC", hyp).split())
    r = "".join(unicodedata.normalize("NFKC", ref).split())
    precisions: list[float] = []
    recalls: list[float] = []
    for n in range(1, max_n + 1):
        hc = _char_ngrams(h, n)
        rc = _char_ngrams(r, n)
        h_total = sum(hc.values())
        r_total = sum(rc.values())
        if h_total == 0 or r_total == 0:
            continue
        matches = sum(min(count, rc[gram]) for gram, count in hc.items())
        precisions.append(matches / h_total)
        recalls.append(matches / r_total)
    if not precisions:
        return 0.0
    p = sum(precisions) / len(precisions)
    rec = sum(recalls) / len(recalls)
    denom = beta**2 * p + rec
    if p + rec == 0 or denom == 0:
        return 0.0
    return 100.0 * (1 + beta**2) * p * rec / denom


def glossary_hits(
    source: str, output: str, entries: Sequence[GlossaryEntry], tgt: str
) -> tuple[int, int]:
    """Return (hits, terms_present) for one segment.

    Terms are matched longest first and each matched span is masked so a
    shorter term nested in a longer one is not counted again.
    """
    masked = unicodedata.normalize("NFKC", source)
    norm_out = normalise(output)
    seen: set[str] = set()
    hits = 0
    present = 0
    for entry in sorted(entries, key=lambda e: len(e.source), reverse=True):
        term = unicodedata.normalize("NFKC", entry.source)
        if not term or term in seen:
            continue
        if term not in masked:
            continue
        seen.add(term)
        masked = masked.replace(term, "\0" * len(term))
        present += 1
        if normalise(entry.target(tgt)) in norm_out:
            hits += 1
    return hits, present


def glossary_adherence(
    pairs: Sequence[tuple[str, str]], entries: Sequence[GlossaryEntry], tgt: str
) -> float | None:
    """Micro-averaged adherence over (source, output) pairs; None if no term present."""
    total_hits = 0
    total_present = 0
    for source, output in pairs:
        h, p = glossary_hits(source, output, entries, tgt)
        total_hits += h
        total_present += p
    if total_present == 0:
        return None
    return total_hits / total_present


def fallback_checks(source: str, output: str) -> dict[str, bool]:
    """Minimal output checks: non-empty and source numbers preserved."""
    out_plain = output.replace(",", "")
    numbers = [m.replace(",", "") for m in _NUMBER_RE.findall(source)]
    return {
        "non_empty": bool(output.strip()),
        "numbers_preserved": all(num in out_plain for num in numbers),
    }


def mean_std(values: Sequence[float]) -> tuple[float, float]:
    """Mean and sample standard deviation (0.0 for a single value)."""
    if not values:
        raise ValueError("mean_std requires at least one value")
    mean = statistics.fmean(values)
    std = statistics.stdev(values) if len(values) >= 2 else 0.0
    return mean, std


def p95(values: Sequence[float]) -> float:
    """95th percentile by nearest rank."""
    if not values:
        raise ValueError("p95 requires at least one value")
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))
    return ordered[rank - 1]


def cost_usd(input_tokens: int, output_tokens: int, price: Any) -> float | None:
    """Cost in USD from per-million-token prices (attrs or mapping); None without price."""
    if price is None:
        return None
    if isinstance(price, dict):
        p_in, p_out = price["input"], price["output"]
    else:
        p_in, p_out = price.input, price.output
    return round((input_tokens * p_in + output_tokens * p_out) / 1e6, 6)
