"""Normalise a model's JSON answer before parsing.

Prompts ask for a bare JSON object, but real models (Claude Haiku 4.5 among
them) often wrap it in a Markdown code fence: ```json ... ```. The fake model
never does, so this was only found by the first real end-to-end run. Only a
fence around the WHOLE answer is removed; anything else is left untouched for
the JSON parser (and the json_valid check) to judge.
"""

from __future__ import annotations

import re

_FENCED = re.compile(r"\A\s*```[A-Za-z0-9_-]*[ \t]*\r?\n(?P<body>.*?)\r?\n?```\s*\Z", re.DOTALL)


def strip_code_fence(text: str) -> str:
    """Return the content of a single code fence wrapping the whole text, else the text."""
    match = _FENCED.match(text)
    return match.group("body") if match else text
