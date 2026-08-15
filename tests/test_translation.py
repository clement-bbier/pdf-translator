"""Unit tests for the translation layer: mock translator, cache, gateway client.

No network access. Run: python tests/test_translation.py
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app import auth  # noqa: E402
from app import config as app_config  # noqa: E402
from app.cache import TranslationCache  # noqa: E402
from app.translator import (  # noqa: E402
    GatewayTranslator,
    MockTranslator,
    TranslationError,
    get_translator,
)

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


# --------------------------------------------------------------------------- #
# MockTranslator
# --------------------------------------------------------------------------- #


def test_mock_translator_deterministic() -> None:
    """Same input, same output across calls."""
    translator = MockTranslator()
    first = translator.translate_blocks(["Bonjour le monde"], "fr", "en")
    second = translator.translate_blocks(["Bonjour le monde"], "fr", "en")
    check(first == second, "MockTranslator is not deterministic")


def test_mock_translator_prefix_and_length() -> None:
    """Output is prefixed with the target tag and grown to ~1.4x the source length."""
    translator = MockTranslator()
    text = "Le prestataire s'engage à livrer les documents convenus"
    [result] = translator.translate_blocks([text], "fr", "en")

    check(result.startswith("[en] "), f"missing target prefix: {result[:20]!r}")
    check(text in result, "original text not preserved in mock translation")

    target_length = int(len(text) * MockTranslator.GROWTH_FACTOR)
    check(
        len(result) >= target_length,
        f"mock translation too short: {len(result)} < {target_length}",
    )


def test_mock_translator_batch_order() -> None:
    """Batch translation preserves order and length."""
    translator = MockTranslator()
    texts = ["premier bloc", "deuxième bloc", "troisième bloc"]
    results = translator.translate_blocks(texts, "fr", "en")

    check(len(results) == len(texts), "batch result length mismatch")
    for original, translated in zip(texts, results):
        check(original in translated, f"batch item lost: {original!r} not in {translated!r}")


def test_mock_translator_empty_text() -> None:
    """A single-character block still produces a non-crashing result."""
    translator = MockTranslator()
    results = translator.translate_blocks(["x"], "fr", "en")
    check(len(results) == 1 and results[0].startswith("[en] "), "single-char block mishandled")


# --------------------------------------------------------------------------- #
# TranslationCache
# --------------------------------------------------------------------------- #


def test_cache_miss_then_hit() -> None:
    """A fresh cache misses, then hits after set_many."""
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "cache.json"
        cache = TranslationCache(cache_path)

        misses = cache.get_many(["hello", "world"], "en", "fr")
        check(misses == {}, "fresh cache should miss everything")

        cache.set_many({"hello": "bonjour", "world": "monde"}, "en", "fr")
        hits = cache.get_many(["hello", "world", "unknown"], "en", "fr")
        check(hits == {"hello": "bonjour", "world": "monde"}, f"unexpected cache hits: {hits}")


def test_cache_persistence() -> None:
    """A new TranslationCache instance reloads entries written by a previous one."""
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "cache.json"
        first = TranslationCache(cache_path)
        first.set_many({"bonjour": "hello"}, "fr", "en")

        second = TranslationCache(cache_path)
        hits = second.get_many(["bonjour"], "fr", "en")
        check(hits == {"bonjour": "hello"}, "cache did not persist across instances")


def test_cache_key_scoped_by_language_pair() -> None:
    """The same text under a different language pair is a separate cache entry."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = TranslationCache(Path(tmp) / "cache.json")
        cache.set_many({"bonjour": "hello"}, "fr", "en")

        hits = cache.get_many(["bonjour"], "fr", "de")
        check(hits == {}, "cache leaked across language pairs")


def test_cache_purge() -> None:
    """purge() clears memory and deletes the backing file."""
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "cache.json"
        cache = TranslationCache(cache_path)
        cache.set_many({"bonjour": "hello"}, "fr", "en")
        check(cache_path.is_file(), "cache file should exist before purge")

        cache.purge()
        check(len(cache) == 0, "cache should be empty after purge")
        check(not cache_path.is_file(), "cache file should be deleted after purge")

        hits = cache.get_many(["bonjour"], "fr", "en")
        check(hits == {}, "purged cache should miss everything")


def test_cache_file_content_is_hash_only() -> None:
    """The persisted JSON never stores the source text, only hash -> translation."""
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "cache.json"
        cache = TranslationCache(cache_path)
        secret_source = "Le montant confidentiel est de 50000 euros"
        cache.set_many({secret_source: "The confidential amount is 50000 euros"}, "fr", "en")

        raw = cache_path.read_text(encoding="utf-8")
        check(secret_source not in raw, "cache file leaks source text in a key")


# --------------------------------------------------------------------------- #
# get_translator factory
# --------------------------------------------------------------------------- #


def test_factory_mock() -> None:
    """provider: mock builds a MockTranslator."""
    translator = get_translator({"provider": "mock"})
    check(isinstance(translator, MockTranslator), "factory did not build a MockTranslator")


def test_factory_gateway_requires_base_url() -> None:
    """provider: gateway without GATEWAY_BASE_URL raises clearly."""
    import os

    previous = os.environ.pop("GATEWAY_BASE_URL", None)
    try:
        raised = False
        try:
            get_translator({"provider": "gateway", "model": "test"})
        except TranslationError:
            raised = True
        check(raised, "factory should raise TranslationError without GATEWAY_BASE_URL")
    finally:
        if previous is not None:
            os.environ["GATEWAY_BASE_URL"] = previous


def test_factory_unknown_provider() -> None:
    """An unrecognised provider raises ValueError."""
    raised = False
    try:
        get_translator({"provider": "bogus"})
    except ValueError:
        raised = True
    check(raised, "factory should reject an unknown provider")


# --------------------------------------------------------------------------- #
# GatewayTranslator (httpx mocked, no real network)
# --------------------------------------------------------------------------- #


def _openai_response(payload: dict[str, str]) -> httpx.Response:
    body = {
        "choices": [
            {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
        ]
    }
    return httpx.Response(200, json=body)


def test_gateway_translator_payload_and_parsing() -> None:
    """The client sends the expected OpenAI-format payload and parses the JSON reply."""
    auth.get_token = lambda: "fake-token"  # type: ignore[attr-defined]
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        body = json.loads(request.content)
        captured["body"] = body
        user_message = body["messages"][1]["content"]
        # Extract the trailing JSON object sent as the user payload.
        json_start = user_message.index("{")
        payload = json.loads(user_message[json_start:])
        return _openai_response({key: f"[fr] {value}" for key, value in payload.items()})

    transport = httpx.MockTransport(handler)
    translator = GatewayTranslator(base_url="https://gateway.example/api", model="test-model")
    _patch_client(translator, transport)

    results = translator.translate_blocks(["Hello", "World"], "en", "fr")

    check(results == ["[fr] Hello", "[fr] World"], f"unexpected translations: {results}")
    check(captured["url"] == "https://gateway.example/api/chat/completions", "wrong endpoint")
    check(captured["auth"] == "Bearer fake-token", "missing/incorrect auth header")
    check(captured["body"]["model"] == "test-model", "model not forwarded")
    check(captured["body"]["messages"][0]["role"] == "system", "missing system prompt")


def test_gateway_translator_missing_key_raises() -> None:
    """A response missing a requested key raises TranslationError."""
    auth.get_token = lambda: "fake-token"  # type: ignore[attr-defined]

    def handler(request: httpx.Request) -> httpx.Response:
        return _openai_response({"0": "only this one"})

    transport = httpx.MockTransport(handler)
    translator = GatewayTranslator(base_url="https://gateway.example/api", model="test-model")
    _patch_client(translator, transport)

    raised = False
    try:
        translator.translate_blocks(["first", "second"], "en", "fr")
    except TranslationError:
        raised = True
    check(raised, "missing key in gateway response should raise TranslationError")


def test_gateway_translator_retries_on_429() -> None:
    """A 429 is retried and eventually succeeds without raising."""
    auth.get_token = lambda: "fake-token"  # type: ignore[attr-defined]
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(429, text="rate limited")
        return _openai_response({"0": "[fr] Bonjour"})

    transport = httpx.MockTransport(handler)
    translator = GatewayTranslator(base_url="https://gateway.example/api", model="test-model")
    _patch_client(translator, transport)
    _patch_sleep()

    results = translator.translate_blocks(["Hello"], "en", "fr")
    check(results == ["[fr] Bonjour"], f"unexpected result after retries: {results}")
    check(attempts["count"] == 3, f"expected 3 attempts, got {attempts['count']}")


def test_gateway_translator_exhausts_retries() -> None:
    """Persistent 500s exhaust retries and raise TranslationError."""
    auth.get_token = lambda: "fake-token"  # type: ignore[attr-defined]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    transport = httpx.MockTransport(handler)
    translator = GatewayTranslator(base_url="https://gateway.example/api", model="test-model")
    _patch_client(translator, transport)
    _patch_sleep()

    raised = False
    try:
        translator.translate_blocks(["Hello"], "en", "fr")
    except TranslationError:
        raised = True
    check(raised, "persistent 5xx should raise TranslationError after retries")


def test_gateway_translator_missing_auth_token() -> None:
    """No gateway session: auth.get_token()'s AuthError surfaces to the caller."""

    def broken_token() -> str:
        raise auth.AuthError("not signed in to the gateway")

    auth.get_token = broken_token  # type: ignore[attr-defined]

    translator = GatewayTranslator(base_url="https://gateway.example/api", model="test-model")
    _patch_client(translator, httpx.MockTransport(lambda request: _openai_response({})))

    raised = False
    try:
        translator.translate_blocks(["Hello"], "en", "fr")
    except auth.AuthError:
        raised = True
    check(raised, "missing auth token should surface as AuthError")


# --------------------------------------------------------------------------- #
# Multimodal path: scanned pages sent as an image
# --------------------------------------------------------------------------- #


def test_translate_page_image_payload_and_parsing() -> None:
    """The image call carries the bearer token and a base64 data URI, and returns text."""
    auth.get_token = lambda: "fake-token"  # type: ignore[attr-defined]
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        body = json.loads(request.content)
        captured["model"] = body["model"]
        content = body["messages"][1]["content"]
        captured["types"] = [part["type"] for part in content]
        captured["image_url"] = next(
            part["image_url"]["url"] for part in content if part["type"] == "image_url"
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Translated page text"}}]},
        )

    transport = httpx.MockTransport(handler)
    translator = GatewayTranslator(base_url="https://gateway.example/api", model="vision-model")
    _patch_client(translator, transport)

    result = translator.translate_page_image(b"\x89PNG\r\n\x1a\nfake-image-bytes", "zh", "fr")

    check(result == "Translated page text", f"unexpected page translation: {result!r}")
    check(captured["auth"] == "Bearer fake-token", f"missing bearer token: {captured['auth']!r}")
    check(captured["model"] == "vision-model", "model not forwarded")
    check(
        captured["types"] == ["text", "image_url"],
        f"unexpected multimodal content parts: {captured['types']}",
    )
    check(
        captured["image_url"].startswith("data:image/png;base64,"),
        f"image should be a base64 data URI: {captured['image_url'][:40]!r}",
    )

    encoded = captured["image_url"].split(",", 1)[1]
    check(
        base64.b64decode(encoded) == b"\x89PNG\r\n\x1a\nfake-image-bytes",
        "the decoded image must match the bytes passed in",
    )


def test_translate_page_image_empty_raises() -> None:
    """An empty image is rejected before any network call."""
    auth.get_token = lambda: "fake-token"  # type: ignore[attr-defined]
    translator = GatewayTranslator(base_url="https://gateway.example/api", model="vision-model")

    raised = False
    try:
        translator.translate_page_image(b"", "zh", "fr")
    except TranslationError:
        raised = True
    check(raised, "an empty page image should raise TranslationError")


def test_translate_page_image_retries_on_503() -> None:
    """The image path reuses the same retry policy as batch translation."""
    auth.get_token = lambda: "fake-token"  # type: ignore[attr-defined]
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    translator = GatewayTranslator(base_url="https://gateway.example/api", model="vision-model")
    _patch_client(translator, transport)
    _patch_sleep()

    result = translator.translate_page_image(b"image-bytes", "zh", "fr")
    check(result == "ok", f"expected the retried result, got {result!r}")
    check(attempts["count"] == 3, f"expected 3 attempts, got {attempts['count']}")


def test_gateway_translator_uses_real_auth_session() -> None:
    """The bearer token comes from a real auth session, not a stubbed get_token."""
    import os

    for key, value in {
        "GATEWAY_TOKEN_ENDPOINT": "https://auth.example/oauth2/token",
        "GATEWAY_CLIENT_ID": "test-client-id",
        "GATEWAY_SCOPES": "openid test.scope",
    }.items():
        os.environ[key] = value

    auth.reset()
    auth._session = auth._Session(  # type: ignore[attr-defined]
        access_token="session-token-xyz",
        refresh_token="refresh-1",
        expires_at=time.monotonic() + 3600,
    )

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return _openai_response({"0": "[fr] Bonjour"})

    transport = httpx.MockTransport(handler)
    translator = GatewayTranslator(base_url="https://gateway.example/api", model="test-model")
    _patch_client(translator, transport)

    translator.translate_blocks(["Hello"], "en", "fr")

    check(
        captured["auth"] == "Bearer session-token-xyz",
        f"token should come from the auth session: {captured['auth']!r}",
    )
    auth.reset()


def _patch_client(translator: GatewayTranslator, transport: httpx.MockTransport) -> None:
    """Monkeypatch httpx.Client used inside the translator to route through transport."""
    import app.translator as translator_module

    original_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    translator_module.httpx.Client = fake_client  # type: ignore[attr-defined]


def _patch_sleep() -> None:
    """Avoid real waiting during retry tests."""
    import app.translator as translator_module

    translator_module.time.sleep = lambda seconds: None  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()

    tests = [
        test_mock_translator_deterministic,
        test_mock_translator_prefix_and_length,
        test_mock_translator_batch_order,
        test_mock_translator_empty_text,
        test_cache_miss_then_hit,
        test_cache_persistence,
        test_cache_key_scoped_by_language_pair,
        test_cache_purge,
        test_cache_file_content_is_hash_only,
        test_factory_mock,
        test_factory_gateway_requires_base_url,
        test_factory_unknown_provider,
        test_gateway_translator_payload_and_parsing,
        test_gateway_translator_missing_key_raises,
        test_gateway_translator_retries_on_429,
        test_gateway_translator_exhausts_retries,
        test_gateway_translator_missing_auth_token,
        test_translate_page_image_payload_and_parsing,
        test_translate_page_image_empty_raises,
        test_translate_page_image_retries_on_503,
        test_gateway_translator_uses_real_auth_session,
    ]

    import app.translator as translator_module

    original_get_token = auth.get_token
    original_client = httpx.Client
    original_sleep = time.sleep
    failures_before = 0
    for test in tests:
        failures_before = len(FAILURES)
        try:
            test()
        except Exception as error:  # noqa: BLE001 - report and continue
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
        finally:
            auth.get_token = original_get_token  # type: ignore[attr-defined]
            translator_module.httpx.Client = original_client  # type: ignore[attr-defined]
            translator_module.time.sleep = original_sleep  # type: ignore[attr-defined]
        status = "OK" if len(FAILURES) == failures_before else "FAIL"
        print(f"[{status}] {test.__name__}")

    print(f"\n{len(tests)} tests, {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
