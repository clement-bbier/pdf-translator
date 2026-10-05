"""run_checks: every deterministic check on one model answer. Pure, no I/O."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.verification import dates, numbers
from app.verification.findings import CheckReport, Finding
from app.verification.glossary import check_glossary
from app.verification.structure import check_json, check_segment_count


def run_checks(
    request: Mapping[str, str],
    answer_text: str,
    src: str,
    tgt: str,
    required_terms: Mapping[str, Sequence[str]] | None = None,
) -> CheckReport:
    """Check a model answer against the request that produced it."""
    parsed, findings = check_json(answer_text)
    if parsed is None:
        return CheckReport({}, tuple(findings))

    findings = list(findings)
    findings.extend(check_segment_count(list(request), parsed))

    translations: dict[str, str] = {}
    ran: list[tuple[str, tuple[str, ...]]] = []
    for key, source in request.items():
        value = parsed.get(key)
        if not isinstance(value, str):
            continue
        translations[key] = value
        names = ["json_valid", "segment_count"]
        terms = (required_terms or {}).get(key)
        segment_findings: list[Finding] = []
        if terms:
            names.append("glossary_terms")
            segment_findings.extend(check_glossary(key, value, terms))
        names.append("numbers")
        segment_findings.extend(numbers.check_numbers(key, source, value, src, tgt))
        names.append("dates")
        segment_findings.extend(dates.check_dates(key, source, value, src, tgt))
        findings.extend(segment_findings)
        ran.append((key, tuple(names)))
    return CheckReport(translations, tuple(findings), tuple(ran))
