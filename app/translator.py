"""Translator interface and its MockTranslator / GatewayTranslator implementations."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app import auth

logger = logging.getLogger(__name__)

# Legal-translation system prompt: absolute fidelity, JSON-only response with the
# same keys as the request, numbering/amounts/dates/proper nouns left untouched.
SYSTEM_PROMPT = (
    "You are a professional legal translator specializing in contracts. "
    "Translate the given text blocks from the source language to the target "
    "language with absolute fidelity to the original meaning and legal intent. "
    "Preserve numbering, monetary amounts, dates, and proper nouns exactly as "
    "written. Never summarize, omit, or add content. "
    "Respond ONLY with a JSON object using the exact same keys as the input, "
    "each value being the translation of the corresponding input text. "
    "Do not include any explanation, markdown formatting, or text outside the "
    "JSON object."
)

# Scanned-page prompt: the page arrives as an image, so there are no blocks and
# no JSON envelope — the model returns the translated text as plain text.
IMAGE_SYSTEM_PROMPT = (
    "You are a professional legal translator specializing in contracts. "
    "The user sends an image of a single scanned contract page. "
    "Read all the text visible in the image and translate it from the source "
    "language to the target language with absolute fidelity to the original "
    "meaning and legal intent. Preserve numbering, monetary amounts, dates, and "
    "proper nouns exactly as written. Never summarize, omit, or add content. "
    "Keep the reading order and the paragraph breaks of the original page. "
    "Respond ONLY with the translated text, with no explanation, no markdown "
    "formatting, and no commentary."
)

# Retry policy for transient gateway errors.
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class TranslationError(Exception):
    """Raised when a batch cannot be translated after retries or on a hard error."""


class Translator(Protocol):
    """Translates a batch of text blocks from a source to a target language."""

    def translate_blocks(self, blocks: list[str], src: str, tgt: str) -> list[str]:
        """Return one translation per input block, same order, same length."""
        ...

    def translate_page_image(self, image_bytes: bytes, src: str, tgt: str) -> str:
        """Return the translation of a whole page sent as a PNG image, as plain text.

        Used for scanned pages, where no text can be extracted and the
        block-by-block path does not apply.
        """
        ...


class MockTranslator:
    """Deterministic, visually identifiable translator for testing without network.

    Output is "[tgt] " + the original text, then padded with repeated words so the
    result is roughly 1.4x the original length — this exercises the rebuilder's
    layout handling (font shrink / truncation) under realistic growth.
    """

    GROWTH_FACTOR = 1.4

    def translate_blocks(self, blocks: list[str], src: str, tgt: str) -> list[str]:
        """Return a deterministic, length-inflated translation for each block."""
        return [self._translate_one(text, tgt) for text in blocks]

    def translate_page_image(self, image_bytes: bytes, src: str, tgt: str) -> str:
        """Return a deterministic multi-paragraph stand-in for a translated scan.

        Keyed on the image size so the same page always yields the same text.
        Multi-paragraph on purpose: it exercises the scanned-page rebuilder's
        paragraph splitting and wrapping without needing a real model.
        """
        if not image_bytes:
            raise TranslationError("page image is empty")

        paragraphs = [
            f"[{tgt}] Scanned page translated from {src} "
            f"({len(image_bytes)} bytes of image data).",
            f"[{tgt}] This is the second paragraph of the mock page "
            "translation, long enough to wrap across several lines when it is "
            "written back into a rebuilt page.",
            f"[{tgt}] Third and final paragraph of the mock translation.",
        ]
        return "\n\n".join(paragraphs)

    def _translate_one(self, text: str, tgt: str) -> str:
        prefix = f"[{tgt}] "
        base = prefix + text
        target_length = int(len(text) * self.GROWTH_FACTOR) + len(prefix)

        words = text.split() or [text]
        grown = base
        index = 0
        while len(grown) < target_length:
            grown += " " + words[index % len(words)]
            index += 1
        return grown


class GatewayTranslator:
    """Translator backed by the internal LLM gateway (OpenAI-compatible chat/completions)."""

    def __init__(self, base_url: str, model: str, *, timeout: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def translate_blocks(self, blocks: list[str], src: str, tgt: str) -> list[str]:
        """Translate a batch of blocks via the gateway; raises TranslationError on failure."""
        if not blocks:
            return []

        payload_map = {str(index): text for index, text in enumerate(blocks)}
        response_map = self._call_with_retry(payload_map, src, tgt)

        missing = [key for key in payload_map if key not in response_map]
        if missing:
            raise TranslationError(
                f"gateway response missing {len(missing)} of {len(payload_map)} keys"
            )

        return [response_map[str(index)] for index in range(len(blocks))]

    def translate_page_image(self, image_bytes: bytes, src: str, tgt: str) -> str:
        """Translate a whole scanned page sent as a PNG image; returns plain text.

        Used for pages with no extractable text, where the block-by-block path
        does not apply. Same auth, retry and error handling as translate_blocks.
        """
        if not image_bytes:
            raise TranslationError("page image is empty")

        encoded = base64.b64encode(image_bytes).decode("ascii")
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": IMAGE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Source language: {src}\nTarget language: {tgt}",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}"},
                        },
                    ],
                },
            ],
        }

        response = self._request_with_retry(body)
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as error:
            raise TranslationError(f"malformed gateway response: {error}") from error

        if not isinstance(content, str):
            raise TranslationError("gateway returned a non-text page translation")
        return content

    def _call_with_retry(self, payload_map: dict[str, str], src: str, tgt: str) -> dict[str, str]:
        return self._parse_response(self._request_with_retry(self._build_body(payload_map, src, tgt)))

    def _build_body(self, payload_map: dict[str, str], src: str, tgt: str) -> dict[str, Any]:
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Source language: {src}\nTarget language: {tgt}\n\n"
                        f"{json.dumps(payload_map, ensure_ascii=False)}"
                    ),
                },
            ],
        }

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        """POST one chat/completions request, mapping transient failures to _RetryableError."""
        headers = {"Authorization": f"Bearer {auth.get_token()}"}

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(
                f"{self._base_url}/chat/completions", json=body, headers=headers
            )

        if response.status_code in RETRYABLE_STATUS:
            raise _RetryableError(f"gateway returned {response.status_code}")
        if response.status_code >= 400:
            raise TranslationError(
                f"gateway returned {response.status_code}: {response.text[:200]}"
            )
        return response

    def _request_with_retry(self, body: dict[str, Any]) -> httpx.Response:
        """Run ``_post`` under the same retry/backoff policy as batch translation."""
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                return self._post(body)
            except _RetryableError as error:
                last_error = error
                if attempt < MAX_RETRIES - 1:
                    delay = BACKOFF_BASE_SECONDS * (2**attempt)
                    logger.warning(
                        "gateway call failed (attempt %d/%d), retrying in %.1fs",
                        attempt + 1,
                        MAX_RETRIES,
                        delay,
                    )
                    time.sleep(delay)
            except httpx.HTTPError as error:
                raise TranslationError(f"gateway request failed: {error}") from error

        raise TranslationError(f"gateway call failed after {MAX_RETRIES} attempts") from last_error

    def _parse_response(self, response: httpx.Response) -> dict[str, str]:
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, ValueError) as error:
            raise TranslationError(f"malformed gateway response: {error}") from error

        if not isinstance(parsed, dict):
            raise TranslationError("gateway response JSON is not an object")

        return {str(key): str(value) for key, value in parsed.items()}


class _RetryableError(Exception):
    """Internal signal: the gateway call failed with a transient error."""


def get_translator(config: dict[str, Any]) -> Translator:
    """Build the Translator described by config.yaml's `provider` key."""
    provider = config.get("provider", "mock")
    if provider == "mock":
        return MockTranslator()
    if provider == "gateway":
        base_url = os.environ.get("GATEWAY_BASE_URL")
        if not base_url:
            raise TranslationError("GATEWAY_BASE_URL is not set")
        return GatewayTranslator(base_url=base_url, model=config.get("model", ""))
    raise ValueError(f"unknown translator provider: {provider!r}")


@dataclass(slots=True)
class JobStats:
    """Counters for one translation job. Never holds document content."""

    total_blocks: int = 0
    unique_blocks: int = 0
    cache_hits: int = 0
    api_calls: int = 0
    estimated_tokens: int = 0

    def record_blocks(self, texts: list[str]) -> None:
        """Register the total and unique block counts for a batch of texts."""
        self.total_blocks += len(texts)
        self.unique_blocks += len(set(texts))

    def record_cache_hits(self, count: int) -> None:
        """Register cache hits, avoiding a real translation call for them."""
        self.cache_hits += count

    def record_api_call(self, texts: list[str]) -> None:
        """Register one API call and estimate its token cost (len // 4)."""
        self.api_calls += 1
        self.estimated_tokens += sum(len(text) // 4 for text in texts)

    def summary(self) -> str:
        """One-line human-readable summary."""
        dedup_pct = (
            100 * (1 - self.unique_blocks / self.total_blocks) if self.total_blocks else 0
        )
        return (
            f"blocks={self.total_blocks} unique={self.unique_blocks} "
            f"(dedup {dedup_pct:.0f}%) cache_hits={self.cache_hits} "
            f"api_calls={self.api_calls} estimated_tokens={self.estimated_tokens}"
        )
