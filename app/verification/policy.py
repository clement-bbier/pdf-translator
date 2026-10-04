"""Retry-then-flag policy: verify a batch answer, retry with the errors, flag the rest."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from app.llm.errors import TranslationError
from app.verification.checks import run_checks
from app.verification.findings import CheckReport, Finding


@dataclass(frozen=True)
class SegmentOutcome:
    """Final state of one segment after verification."""

    key: str
    translation: str | None  # None = missing from every attempt
    passed: tuple[str, ...]
    failed: tuple[str, ...]
    findings: tuple[Finding, ...]
    retried: bool
    flagged: bool


@dataclass(frozen=True)
class BatchVerification:
    """Outcome of verifying one batch. Holds no content except the translations."""

    segments: tuple[SegmentOutcome, ...]
    attempts: int
    batch_findings: tuple[Finding, ...]  # from the last attempt
    input_ids: tuple[str, ...]

    @property
    def verified(self) -> int:
        """Segments checked."""
        return len(self.segments)

    @property
    def retried(self) -> int:
        """Segments re-sent in a retry."""
        return len(self.segments) if self.attempts > 1 else 0

    @property
    def flagged(self) -> tuple[SegmentOutcome, ...]:
        """Segments that still need human review."""
        return tuple(outcome for outcome in self.segments if outcome.flagged)


def _unparseable(report: CheckReport) -> bool:
    return any(f.check == "json_valid" and f.key is None for f in report.batch_findings)


def verify_with_retry(
    request: Mapping[str, str],
    src: str,
    tgt: str,
    *,
    attempt: Callable[[Sequence[Finding]], tuple[str, str]],
    max_retries: int,
    required_terms: Mapping[str, Sequence[str]] | None = None,
) -> BatchVerification:
    """Run attempt(), check it, and retry up to max_retries times with the findings.

    attempt(findings) returns (answer_text, input_id); the first call gets ().
    Per segment the best attempt is kept: fewest findings, a present value
    beating a missing one, ties going to the latest attempt.
    """
    reports: list[CheckReport] = []
    input_ids: list[str] = []
    corrections: Sequence[Finding] = ()
    for _ in range(max(0, max_retries) + 1):
        answer_text, input_id = attempt(corrections)
        input_ids.append(input_id)
        report = run_checks(request, answer_text, src, tgt, required_terms)
        reports.append(report)
        if report.ok:
            break
        corrections = report.findings

    parseable = [report for report in reports if not _unparseable(report)]
    if not parseable:
        raise TranslationError(
            f"model answer failed verification: not a JSON object after {len(reports)} attempt(s)"
        )

    retried = len(reports) > 1
    outcomes: list[SegmentOutcome] = []
    for key in request:
        best = parseable[-1]
        best_rank: tuple[bool, int] | None = None
        for report in parseable:
            rank = (key not in report.translations, len(report.findings_for(key)))
            if best_rank is None or rank <= best_rank:
                best, best_rank = report, rank
        translation = best.translations.get(key)
        findings = best.findings_for(key)
        passed, failed = best.checks_for(key)
        if translation is None and not failed:
            failed = ("segment_count",)
        outcomes.append(
            SegmentOutcome(
                key=key,
                translation=translation,
                passed=passed,
                failed=failed,
                findings=findings,
                retried=retried,
                flagged=translation is None or bool(findings),
            )
        )

    return BatchVerification(
        segments=tuple(outcomes),
        attempts=len(reports),
        batch_findings=reports[-1].batch_findings,
        input_ids=tuple(input_ids),
    )
