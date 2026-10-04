"""Translator built on any LLMProvider."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.llm.base import LLMProvider, LLMRequest, request_fingerprint
from app.llm.errors import TranslationError
from app.llm.prompts import PromptBuilder, load_prompt
from app.verification.glossary import collect_required_terms
from app.verification.policy import BatchVerification, verify_with_retry
from app.verification.retry_prompt import retry_messages
from app.verification.trace import VerificationTrace

if TYPE_CHECKING:
    from app.rag.context import RagContext


class LLMTranslator:
    """Implements the Translator protocol on top of an LLMProvider.

    Per batch: the RAG context (glossary, translation memory) is rendered once;
    its text goes into the prompt, its provider counts into the request (for the
    trace) and its required terms into verification. With verification enabled,
    the output is checked by code, retried once with the failed checks listed,
    and flagged for human review if it still fails.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int = 4096,
        prompt_builder: PromptBuilder | None = None,
        context_provider: RagContext | None = None,
        verification: Any = None,
        term_providers: Sequence[object] = (),
    ) -> None:
        self._provider = provider
        self._context_provider = context_provider
        self.model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._prompts = prompt_builder or PromptBuilder("v1")
        self.prompt_version = self._prompts.version
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
        VerificationSettings); None disables verification. ``term_providers``
        are extra sources of required terms, on top of the RAG context.
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

    def _invalidate(self, request: LLMRequest) -> None:
        """Ask a caching provider to forget a response we rejected."""
        invalidate = getattr(self._provider, "invalidate", None)
        if invalidate is not None:
            invalidate(request)

    def _prepare(
        self, payload_map: dict[str, str], blocks: list[str], src: str, tgt: str
    ) -> tuple[list[dict], dict[str, int], dict[str, list[str]]]:
        """Messages, context sources and required terms for one batch."""
        context_text = ""
        sources: dict[str, int] = {}
        required: dict[str, list[str]] = {}
        if self._context_provider is not None:
            rendered = self._context_provider.render(blocks, src, tgt)
            context_text = rendered.text
            sources = dict(rendered.sources)
            required = {key: list(terms) for key, terms in rendered.required_terms.items()}
        for key, terms in collect_required_terms(
            self._term_providers, payload_map, src, tgt
        ).items():
            bucket = required.setdefault(key, [])
            bucket.extend(term for term in terms if term not in bucket)
        messages = self._prompts.blocks_messages(payload_map, src, tgt, extra_context=context_text)
        return messages, sources, required

    def translate_blocks(self, blocks: list[str], src: str, tgt: str) -> list[str]:
        """Translate a batch of blocks in one call; raises TranslationError on failure."""
        if not blocks:
            return []

        self.last_verification = None
        payload_map = {str(index): text for index, text in enumerate(blocks)}
        messages, sources, required = self._prepare(payload_map, blocks, src, tgt)
        if self._verification is not None and self._verification.enabled:
            return self._translate_verified(
                payload_map, blocks, src, tgt, messages, sources, required
            )

        request = self._request(messages)
        request.context_sources = sources
        response = self._provider.complete(request)

        try:
            parsed = json.loads(response.text)
        except ValueError as error:
            self._invalidate(request)
            raise TranslationError(f"malformed model response: {error}") from error
        if not isinstance(parsed, dict):
            self._invalidate(request)
            raise TranslationError("model response JSON is not an object")

        missing = [key for key in payload_map if key not in parsed]
        if missing:
            self._invalidate(request)
            raise TranslationError(
                f"model response missing {len(missing)} of {len(payload_map)} keys"
            )
        return [str(parsed[str(index)]) for index in range(len(blocks))]

    def _translate_verified(
        self,
        payload_map: dict[str, str],
        blocks: list[str],
        src: str,
        tgt: str,
        base_messages: list[dict],
        sources: dict[str, int],
        required_terms: dict[str, list[str]],
    ) -> list[str]:
        """Translate with code checks, one retry round with the errors, then flag."""

        def attempt(findings: Sequence[Any]) -> tuple[str, str]:
            messages = base_messages
            if findings:
                messages = retry_messages(
                    base_messages, load_prompt(self.prompt_version, "retry_corrections"), findings
                )
            request = self._request(messages)
            request.context_sources = sources
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
            self._request(self._prompts.page_image_messages(image_bytes, src, tgt))
        )
        return response.text
