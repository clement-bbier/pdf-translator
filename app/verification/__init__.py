"""Deterministic verification of LLM translations (code checks, no LLM call).

Checks run on every model answer: valid JSON, segment count, glossary terms,
numbers and dates. They return findings; the retry-then-flag policy that uses
them lives elsewhere. Nothing here does I/O or logs document content.
"""

from app.verification.checks import run_checks
from app.verification.findings import CHECK_NAMES, CheckReport, Finding
from app.verification.glossary import TermProvider, collect_required_terms
from app.verification.settings import VerificationConfig, verification_config

__all__ = [
    "CHECK_NAMES",
    "CheckReport",
    "Finding",
    "TermProvider",
    "VerificationConfig",
    "collect_required_terms",
    "run_checks",
    "verification_config",
]
