"""Translator built on any LLMProvider."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.llm.base import LLMProvider, LLMRequest, request_fingerprint
from app.llm.errors import TranslationError
from app.llm.prompts import blocks_messages, page_image_messages
from app.verification.glossary import collect_required_terms
from app.verification.policy import BatchVerification, verify_with_retry
from app.verification.retry_prompt import load_retry_template, retry_messages
from app.verification.trace import VerificationTrace


class LLMTranslator:
    """Implements the Translator protocol on top of an LLMProvider."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int = 4096,
        prompt_version: str = "v1",
        verification: Any = None,
        term_providers: Sequence[object] = (),
    ) -> None:
        self._provider = provider
        self.model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self.prompt_version = prompt_version
        self._verification: Any = None
        self._term_providers: Sequence[object] = ()
        self._verification_trace: VerificationTrace | None = None
        self.last_verification: BatchVerification | None = None
        self.enable_verification(verification, term_providers=term_providers)

    def enable_verification(
        self,
        settings: Any,
        *,
        term_providers: Sequence[object] = (),
        trace_path: str | Path | None = None,
    ) -> None:
        """Turn the retry-then-flag verification on or off.

        ``settings`` has ``enabled`` and ``max_retries`` (VerificationConfig or
        VerificationSettings); None disables verification.
        """
        self._verification = settings
        self._term_providers = tuple(term_providers)
        self._verification_trace = VerificationTrace(trace_path) if trace_path else None

    def _request(self, messages: list[dict]) -> LLMRequest:
        return LLMRequest(
            messages=messages,
            model=self.model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            prompt_version=self.prompt_version,
        )

    def translate_blocks(self, blocks: list[str], src: str, tgt: str) -> list[str]:
        """Translate a batch of blocks in one call; raises TranslationError on failure."""
        if not blocks:
            return []

        self.last_verification = None
        payload_map = {str(index): text for index, text in enumerate(blocks)}
        if self._verification is not None and self._verification.enabled:
            return self._translate_verified(payload_map, blocks, src, tgt)
        response = self._provider.complete(self._request(blocks_messages(payload_map, src, tgt)))

        try:
            parsed = json.loads(response.text)
        except ValueError as error:
            raise TranslationError(f"malformed model response: {error}") from error
        if not isinstance(parsed, dict):
            raise TranslationError("model response JSON is not an object")

        missing = [key for key in payload_map if key not in parsed]
        if missing:
            raise TranslationError(
                f"model response missing {len(missing)} of {len(payload_map)} keys"
            )
        return [str(parsed[str(index)]) for index in range(len(blocks))]

    def _translate_verified(
        self, payload_map: dict[str, str], blocks: list[str], src: str, tgt: str
    ) -> list[str]:
        """Translate with code checks, one retry round with the errors, then flag."""
        base_messages = blocks_messages(payload_map, src, tgt)
        required_terms = collect_required_terms(self._term_providers, payload_map, src, tgt)

        def attempt(findings: Sequence[Any]) -> tuple[str, str]:
            messages = base_messages
            if findings:
                messages = retry_messages(
                    base_messages, load_retry_template(self.prompt_version), findings
                )
            request = self._request(messages)
            response = self._provider.complete(request)
            return response.text, request_fingerprint(request)

        verification = verify_with_retry(
            payload_map,
            src,
            tgt,
            attempt=attempt,
            max_retries=self._verification.max_retries,
            required_terms=required_terms,
        )
        self.last_verification = verification
        if self._verification_trace is not None:
            self._verification_trace.record(
                verification, model=self.model, prompt_version=self.prompt_version
            )
        # A missing translation falls back to the source only to satisfy the
        # protocol; the pipeline reads last_verification and never uses it.
        return [
            outcome.translation if outcome.translation is not None else blocks[index]
            for index, outcome in enumerate(verification.segments)
        ]

    def translate_page_image(self, image_bytes: bytes, src: str, tgt: str) -> str:
        """Translate a scanned page sent as a PNG image; returns plain text."""
        if not image_bytes:
            raise TranslationError("page image is empty")
        response = self._provider.complete(
            self._request(page_image_messages(image_bytes, src, tgt))
        )
        return response.text
