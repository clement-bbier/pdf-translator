"""Retry prompt: the corrections appended to a request after a failed check."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.verification.findings import Finding

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


@lru_cache
def load_retry_template(version: str) -> str:
    """Read prompts/<version>/retry_corrections.md, trailing newlines stripped.

    Swap for app.llm.prompts.load_prompt(version, "retry_corrections") at the
    Phase 1 merge.
    """
    path = PROMPTS_DIR / version / "retry_corrections.md"
    try:
        return path.read_text(encoding="utf-8").rstrip("\n")
    except OSError as error:
        relative = path.relative_to(PROMPTS_DIR.parent).as_posix()
        raise ValueError(f"retry prompt not found: {relative}") from error


def render_corrections(findings: Sequence[Finding]) -> str:
    """One line per finding. Braces are neutralised so no stray JSON object appears."""
    lines = []
    for finding in findings:
        where = "whole answer" if finding.key is None else f"segment {finding.key}"
        message = finding.message.replace("{", "(").replace("}", ")")
        lines.append(f"- {where} [{finding.check}]: {message}")
    return "\n".join(lines)


def retry_messages(
    messages: list[dict[str, Any]], template: str, findings: Sequence[Finding]
) -> list[dict[str, Any]]:
    """Copy of the messages with the corrections appended to the LAST user message."""
    copied = copy.deepcopy(messages)
    suffix = "\n\n" + template.replace("{corrections}", render_corrections(findings))
    for message in reversed(copied):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = content + suffix
        elif isinstance(content, list):
            content.append({"type": "text", "text": suffix.lstrip("\n")})
        break
    return copied
