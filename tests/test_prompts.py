"""Tests for app.llm.prompts: versioned prompt files and the PromptBuilder.

No network access. Run: python tests/test_prompts.py
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402
from app.llm.prompts import PromptBuilder, load_prompt  # noqa: E402

FAILURES: list[str] = []

# Hard-coded copies of the t1 prompts: the v1 files must render byte-identically.
EXPECTED_SYSTEM = (
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
EXPECTED_IMAGE_SYSTEM = (
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


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


class _StubContext:
    def __init__(self, text: str) -> None:
        self._text = text
        self.seen: list[str] = []

    def context_for(self, segments: list[str], src: str, tgt: str) -> str:
        self.seen = segments
        return self._text


def test_blocks_messages_byte_identical() -> None:
    """v1 blocks messages equal the former hard-coded prompt and user format."""
    payload = {"0": "你好 {x}", "1": "Article 5"}
    messages = PromptBuilder("v1").blocks_messages(payload, "zh", "fr")
    expected_user = "Source language: zh\nTarget language: fr\n\n" + json.dumps(
        payload, ensure_ascii=False
    )
    check(
        messages
        == [
            {"role": "system", "content": EXPECTED_SYSTEM},
            {"role": "user", "content": expected_user},
        ],
        f"blocks messages differ: {messages!r}",
    )


def test_page_image_messages_byte_identical() -> None:
    """v1 page-image messages equal the former hard-coded prompt and parts."""
    image = b"\x89PNG-fake"
    messages = PromptBuilder("v1").page_image_messages(image, "ja", "en")
    expected = [
        {"role": "system", "content": EXPECTED_IMAGE_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Source language: ja\nTarget language: en"},
                {
                    "type": "image",
                    "media_type": "image/png",
                    "data": base64.b64encode(image).decode("ascii"),
                },
            ],
        },
    ]
    check(messages == expected, f"page image messages differ: {messages!r}")


def test_load_prompt_cached() -> None:
    """load_prompt is lru_cached."""
    load_prompt("v1", "translate_blocks.system")
    before = load_prompt.cache_info().hits
    load_prompt("v1", "translate_blocks.system")
    check(load_prompt.cache_info().hits == before + 1, "load_prompt did not hit its cache")


def test_context_provider_before_payload() -> None:
    """Context text appears between the language header and the payload."""
    stub = _StubContext("GLOSSARY: foo = bar")
    other = _StubContext("")
    builder = PromptBuilder("v1", context_providers=[stub, other])
    payload = {"0": "foo"}
    user = builder.blocks_messages(payload, "en", "fr")[1]["content"]
    payload_json = json.dumps(payload)
    check("GLOSSARY: foo = bar" in user, "context missing")
    check(
        user
        == f"Source language: en\nTarget language: fr\n\nGLOSSARY: foo = bar\n\n{payload_json}",
        f"unexpected user message: {user!r}",
    )
    check(stub.seen == ["foo"], f"provider got wrong segments: {stub.seen}")


def test_payload_not_resubstituted() -> None:
    """Placeholders inside the payload or the context are left untouched."""
    payload = {"0": "keep {context} and {src} as is"}
    user = PromptBuilder("v1").blocks_messages(payload, "en", "fr")[1]["content"]
    check("keep {context} and {src} as is" in user, f"payload was re-substituted: {user!r}")

    builder = PromptBuilder("v1", context_providers=[_StubContext("see {payload}")])
    user = builder.blocks_messages({"0": "x"}, "en", "fr")[1]["content"]
    check("see {payload}" in user, f"context was re-substituted: {user!r}")


def test_unknown_version() -> None:
    """An unknown version or file raises ValueError naming the path."""
    builder = PromptBuilder("v999")
    try:
        builder.blocks_messages({"0": "x"}, "en", "fr")
        raised = None
    except ValueError as error:
        raised = str(error)
    check(raised is not None and "prompts/v999" in raised, f"unknown version error: {raised!r}")

    try:
        load_prompt("v1", "nope")
        missing = False
    except ValueError:
        missing = True
    check(missing, "unknown prompt file should raise ValueError")


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()
    tests = [
        test_blocks_messages_byte_identical,
        test_page_image_messages_byte_identical,
        test_load_prompt_cached,
        test_context_provider_before_payload,
        test_payload_not_resubstituted,
        test_unknown_version,
    ]
    for test in tests:
        before = len(FAILURES)
        try:
            test()
        except Exception as error:  # noqa: BLE001 - report and continue
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
        print(f"[{'OK' if len(FAILURES) == before else 'FAIL'}] {test.__name__}")

    print(f"\n{len(tests)} tests, {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
