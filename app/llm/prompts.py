"""Translation prompts and message builders (temporary home, see the prompt task)."""

from __future__ import annotations

import base64
import json
from typing import Any

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


def blocks_messages(
    payload_map: dict[str, str], src: str, tgt: str, context: str = ""
) -> list[dict[str, Any]]:
    """System + user messages for a batch of text blocks.

    A non-empty ``context`` (retrieved terminology or examples) goes between the
    language header and the JSON payload; an empty one leaves the prompt unchanged.
    """
    context_part = f"{context}\n\n" if context else ""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Source language: {src}\nTarget language: {tgt}\n\n"
                f"{context_part}"
                f"{json.dumps(payload_map, ensure_ascii=False)}"
            ),
        },
    ]


def page_image_messages(image_bytes: bytes, src: str, tgt: str) -> list[dict[str, Any]]:
    """System + user messages for a scanned page sent as a PNG image."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return [
        {"role": "system", "content": IMAGE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Source language: {src}\nTarget language: {tgt}"},
                {"type": "image", "media_type": "image/png", "data": encoded},
            ],
        },
    ]
