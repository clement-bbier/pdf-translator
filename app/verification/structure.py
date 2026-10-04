"""Structural checks: valid JSON object, expected keys."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from app.llm.answer import strip_code_fence
from app.verification.findings import Finding


def check_json(answer_text: str) -> tuple[dict[str, object] | None, list[Finding]]:
    """Parse the answer as a JSON object and check that values are strings."""
    try:
        parsed = json.loads(strip_code_fence(answer_text))
    except (ValueError, TypeError):
        return None, [Finding("json_valid", None, "answer is not valid JSON")]
    if not isinstance(parsed, dict):
        return None, [Finding("json_valid", None, "answer is not a JSON object")]
    findings = [
        Finding("json_valid", str(key), "value is not a string")
        for key, value in parsed.items()
        if not isinstance(value, str)
    ]
    return parsed, findings


def check_segment_count(request_keys: Sequence[str], parsed: Mapping[str, object]) -> list[Finding]:
    """Report missing and unexpected keys."""
    findings = [
        Finding("segment_count", key, "segment missing from the answer")
        for key in request_keys
        if key not in parsed
    ]
    expected = set(request_keys)
    findings.extend(
        Finding("segment_count", None, f"unexpected key '{key}' in the answer")
        for key in parsed
        if key not in expected
    )
    return findings
