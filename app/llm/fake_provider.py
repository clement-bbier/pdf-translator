"""Deterministic offline provider used by tests and the evaluation harness.

It understands the prompt format built by app.llm.prompts: a user message
carrying ``Target language: <code>`` and a JSON object of segments. Glossary
lines are read from any message text, so a glossary-injecting provider (phase 2
GlossaryProvider) must render each pair as ``- <source> => <target>``, one per
line (see GLOSSARY_LINE_RE).
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.llm.base import LLMRequest, LLMResponse

GLOSSARY_LINE_RE = r"^- (.+?) => (.+)$"
_TARGET_RE = re.compile(r"^Target language: (\S+)", re.MULTILINE)
_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")

_MONTHS = {
    "en": [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ],
    "fr": [
        "janvier",
        "février",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
    ],
}


def _texts(message: dict[str, Any]) -> list[str]:
    content = message["content"]
    if isinstance(content, str):
        return [content]
    return [p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text"]


def _image_length(message: dict[str, Any]) -> int:
    content = message["content"]
    if isinstance(content, str):
        return 0
    return sum(len(p.get("data", "")) for p in content if p.get("type") == "image")


def _extract_segments(text: str) -> dict[str, Any] | None:
    """Return the dict at the LAST '{' position that parses as JSON, if any."""
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    for position, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, position)
        except ValueError:
            continue
        if isinstance(value, dict):
            found = value
    return found


class FakeProvider:
    """Offline LLMProvider: prefixes values with ``[tgt] `` and applies simple rules."""

    MODEL_ID = "fake-model"

    def __init__(
        self,
        *,
        drop_numbers: bool = False,
        wrong_segment_count: bool = False,
        invalid_json: bool = False,
        fenced_json: bool = False,
    ) -> None:
        self._drop_numbers = drop_numbers
        self._wrong_segment_count = wrong_segment_count
        self._invalid_json = invalid_json
        # Real models often wrap JSON in a Markdown fence; reproduce it on demand.
        self._fenced_json = fenced_json

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Return a deterministic answer for the request."""
        all_text = "\n".join(t for m in request.messages for t in _texts(m))
        last_user = next((m for m in reversed(request.messages) if m["role"] == "user"), None)
        last_text = "\n".join(_texts(last_user)) if last_user else ""

        target_match = _TARGET_RE.search(last_text)
        tgt = target_match.group(1) if target_match else ""

        segments = _extract_segments(last_text)
        if segments is None:
            image_len = _image_length(last_user) if last_user else 0
            text = self._page_text(tgt, image_len)
        else:
            glossary = self._glossary(all_text)
            result = {
                key: f"[{tgt}] " + self._transform(str(value), tgt, glossary)
                for key, value in segments.items()
            }
            if self._drop_numbers:
                result = {k: re.sub(r"[0-9]", "", v) for k, v in result.items()}
            if self._wrong_segment_count:
                if len(result) > 1:
                    result.pop(list(result)[-1])
                else:
                    result["999"] = f"[{tgt}] extra"
            text = json.dumps(result, ensure_ascii=False)
            if self._invalid_json:
                text = text[:-1]
            if self._fenced_json:
                text = f"```json\n{text}\n```"

        return LLMResponse(
            text=text,
            model=self.MODEL_ID,
            input_tokens=sum(len(t) for m in request.messages for t in _texts(m)) // 4,
            output_tokens=len(text) // 4,
            finish_reason="end_turn",
        )

    @staticmethod
    def _glossary(text: str) -> list[tuple[str, str]]:
        pairs = re.findall(GLOSSARY_LINE_RE, text, re.MULTILINE)
        return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)

    @staticmethod
    def _transform(value: str, tgt: str, glossary: list[tuple[str, str]]) -> str:
        for source, target in glossary:
            value = value.replace(source, target)
        months = _MONTHS.get(tgt)
        if months is not None:

            def render(match: re.Match[str]) -> str:
                year, month, day = match.groups()
                index = int(month) - 1
                if not 0 <= index < 12:
                    return match.group(0)
                return f"{int(day)} {months[index]} {year}"

            value = _DATE_RE.sub(render, value)
        return value

    @staticmethod
    def _page_text(tgt: str, image_len: int) -> str:
        return "\n\n".join(
            [
                f"[{tgt}] Scanned page translated ({image_len} characters of image data).",
                f"[{tgt}] This is the second paragraph of the fake page translation.",
                f"[{tgt}] Third and final paragraph of the fake translation.",
            ]
        )
