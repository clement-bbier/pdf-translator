"""Versioned prompts (prompts/<version>/*.md) and the PromptBuilder that renders them."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"

_PLACEHOLDER = re.compile(r"\{(src|tgt|context|payload)\}")


class ContextProvider(Protocol):
    """Supplies extra prompt context (glossary, translation memory, ...) for a batch."""

    def context_for(self, segments: list[str], src: str, tgt: str) -> str:
        """Return a text block to put before the payload, or '' for nothing."""
        ...


@cache
def load_prompt(version: str, name: str) -> str:
    """Read prompts/<version>/<name>.md (UTF-8), trailing newlines stripped."""
    path = PROMPTS_DIR / version / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as error:
        relative = path.relative_to(REPO_ROOT).as_posix()
        raise ValueError(f"prompt file not found: {relative}") from error


def _render(template: str, values: dict[str, str]) -> str:
    """Single-pass substitution: substituted values are never scanned again."""
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


class PromptBuilder:
    """Builds provider-neutral messages from a prompt version and context providers."""

    def __init__(
        self, version: str = "v1", context_providers: Sequence[ContextProvider] = ()
    ) -> None:
        self.version = version
        self._context_providers = tuple(context_providers)

    def _context(self, segments: list[str], src: str, tgt: str, extra: str = "") -> str:
        parts = [provider.context_for(segments, src, tgt) for provider in self._context_providers]
        joined = "\n\n".join(part for part in [*parts, extra] if part)
        return f"{joined}\n\n" if joined else ""

    def blocks_messages(
        self, payload_map: dict[str, str], src: str, tgt: str, extra_context: str = ""
    ) -> list[dict[str, Any]]:
        """System + user messages for a batch of text blocks.

        ``extra_context`` is context the caller already rendered (the RAG block,
        which LLMTranslator renders once per batch to also record its sources);
        it follows the builder's own providers. Empty context leaves the prompt
        byte-identical to the context-free one.
        """
        user = _render(
            load_prompt(self.version, "translate_blocks.user"),
            {
                "src": src,
                "tgt": tgt,
                "context": self._context(list(payload_map.values()), src, tgt, extra_context),
                "payload": json.dumps(payload_map, ensure_ascii=False),
            },
        )
        return [
            {"role": "system", "content": load_prompt(self.version, "translate_blocks.system")},
            {"role": "user", "content": user},
        ]

    def page_image_messages(self, image_bytes: bytes, src: str, tgt: str) -> list[dict[str, Any]]:
        """System + user messages for a scanned page sent as a PNG image."""
        text = _render(
            load_prompt(self.version, "translate_page_image.user"),
            {"src": src, "tgt": tgt, "context": "", "payload": ""},
        )
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return [
            {"role": "system", "content": load_prompt(self.version, "translate_page_image.system")},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image", "media_type": "image/png", "data": encoded},
                ],
            },
        ]
