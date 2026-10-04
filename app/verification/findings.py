"""Finding and CheckReport types shared by every verification check."""

from __future__ import annotations

from dataclasses import dataclass

CHECK_NAMES = ("json_valid", "segment_count", "glossary_terms", "numbers", "dates")


@dataclass(frozen=True, slots=True)
class Finding:
    """One problem found by a check.

    key is None for a batch-level finding (whole answer), otherwise the request
    key of the segment ('0', '1', ...).
    """

    check: str
    key: str | None
    message: str


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Result of run_checks for one model answer."""

    translations: dict[str, str]
    findings: tuple[Finding, ...]
    # Checks that ran for each key (internal bookkeeping for checks_for).
    ran: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def ok(self) -> bool:
        """True when no check reported anything."""
        return not self.findings

    @property
    def batch_findings(self) -> tuple[Finding, ...]:
        """Findings that concern the whole answer."""
        return tuple(f for f in self.findings if f.key is None)

    def failed_keys(self) -> set[str]:
        """Keys of the segments with at least one finding."""
        return {f.key for f in self.findings if f.key is not None}

    def findings_for(self, key: str) -> tuple[Finding, ...]:
        """Findings that concern one segment."""
        return tuple(f for f in self.findings if f.key == key)

    def checks_for(self, key: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return (passed, failed) check names for a segment.

        Only the checks that actually ran for the segment are considered.
        """
        ran: tuple[str, ...] = ()
        for ran_key, names in self.ran:
            if ran_key == key:
                ran = names
                break
        failed_set = {f.check for f in self.findings_for(key)}
        names = tuple(dict.fromkeys((*ran, *sorted(failed_set))))
        failed = tuple(n for n in names if n in failed_set)
        passed = tuple(n for n in names if n not in failed_set)
        return passed, failed
