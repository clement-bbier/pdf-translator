"""Tests for the validated llm config, the provider factory and model/prompt stats.

No network access. Run: python tests/test_llm_config.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

import app.llm.gateway_provider as gateway_module  # noqa: E402
import app.pipeline as pipeline_module  # noqa: E402
from app import auth  # noqa: E402
from app import config as app_config  # noqa: E402
from app.config import ConfigError, llm_settings  # noqa: E402
from app.llm.translator import LLMTranslator  # noqa: E402
from app.translator import JobStats, MockTranslator, get_translator  # noqa: E402
from tests import make_fixtures  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def _raises_config_error(config: dict) -> str | None:
    try:
        llm_settings(config)
    except ConfigError as error:
        return str(error)
    return None


def _isolated(config: dict, tmp: str) -> dict:
    """Point tracing and cache into tmp so nothing lands in the repo."""
    config = dict(config)
    config["tracing"] = {"enabled": True, "path": str(Path(tmp) / "trace.jsonl")}
    config["cache"] = {"enabled": True, "path": str(Path(tmp) / "llm-cache.json")}
    return config


def test_committed_config() -> None:
    """The committed config.yaml validates and carries the pinned values."""
    config = app_config.load_config(app_config.DEFAULT_CONFIG_PATH)
    check(config["provider"] == "mock", "provider should stay mock")
    llm = llm_settings(config)
    check(llm.model == "claude-haiku-4-5-20251001", f"model: {llm.model}")
    check(llm.temperature == 0, f"temperature: {llm.temperature}")
    check(llm.prompt_version == "v1", f"prompt_version: {llm.prompt_version}")
    first = llm.compare_models[0]
    check(first.model == "claude-sonnet-5-5" and first.temperature is None, f"compare: {first}")
    check(llm.prices["claude-sonnet-5-5"].output == 10.0, "price table not loaded")
    tracing = app_config.tracing_settings(config)
    check(Path(tracing.path).is_absolute(), f"tracing path not resolved: {tracing.path}")


def test_invalid_values() -> None:
    """Out-of-range values, unknown keys and bad versions raise ConfigError."""
    for bad in (
        {"temperature": 2},
        {"max_tokens": 0},
        {"unknown_key": 1},
        {"prompt_version": "one"},
    ):
        message = _raises_config_error({"llm": bad})
        check(message is not None and "'llm'" in message, f"{bad} should raise: {message!r}")
    try:
        app_config.tracing_settings({"tracing": {"nope": 1}})
        raised = False
    except ConfigError as error:
        raised = "'tracing'" in str(error)
    check(raised, "tracing extra key accepted")


def test_defaults_without_section() -> None:
    """A config without llm section yields the defaults."""
    llm = llm_settings({"provider": "mock"})
    check(llm.model == "claude-haiku-4-5-20251001" and llm.temperature == 0, "defaults wrong")
    check(llm.compare_models[0].model == "claude-sonnet-5-5", "default compare_models wrong")
    check(app_config.cache_settings({}).enabled, "cache default should be enabled")
    check(not app_config.verification_settings({}).enabled, "verification default")
    check(not app_config.rag_settings({}).glossary, "rag default")


def test_gateway_request_carries_model_and_temperature() -> None:
    """get_translator(gateway) sends the top-level model and llm.temperature."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        content = json.dumps({"0": "bonjour"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    original_client = gateway_module.httpx.Client
    original_token = auth.get_token
    saved_url = os.environ.get("GATEWAY_BASE_URL")
    os.environ["GATEWAY_BASE_URL"] = "https://gateway.example/api"
    auth.get_token = lambda: "fake-token"  # type: ignore[assignment]

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    gateway_module.httpx.Client = fake_client  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            config = _isolated({"provider": "gateway", "model": "gw-model"}, tmp)
            translator = get_translator(config)
            result = translator.translate_blocks(["hello"], "en", "fr")
    finally:
        gateway_module.httpx.Client = original_client  # type: ignore[assignment]
        auth.get_token = original_token  # type: ignore[assignment]
        if saved_url is None:
            os.environ.pop("GATEWAY_BASE_URL", None)
        else:
            os.environ["GATEWAY_BASE_URL"] = saved_url

    check(result == ["bonjour"], f"unexpected result: {result}")
    body = captured.get("body", {})
    check(body.get("temperature") == 0, f"temperature not sent: {body.get('temperature')!r}")
    check(body.get("model") == "gw-model", f"model not sent: {body.get('model')!r}")


def test_fake_and_anthropic_translators() -> None:
    """fake and anthropic build LLMTranslators with prompt_version v1."""
    saved = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            fake = get_translator(_isolated({"provider": "fake"}, tmp))
            anthropic_t = get_translator(_isolated({"provider": "anthropic"}, tmp))
    finally:
        if saved is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved
    for name, translator in (("fake", fake), ("anthropic", anthropic_t)):
        check(isinstance(translator, LLMTranslator), f"{name} is not an LLMTranslator")
        check(translator.prompt_version == "v1", f"{name} prompt_version")  # type: ignore[attr-defined]
    check(fake.model == "fake-model", f"fake model: {fake.model}")  # type: ignore[attr-defined]
    check(
        anthropic_t.model == "claude-haiku-4-5-20251001",  # type: ignore[attr-defined]
        "anthropic model",
    )


def test_stats_model_and_prompt_version() -> None:
    """A fake pipeline run reports model/prompt_version; a mock run is unchanged."""
    fixture = make_fixtures.FIXTURES_DIR / "simple.pdf"
    original_cache_cls = pipeline_module.TranslationCache
    job_dirs: list[Path] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_module.TranslationCache = lambda: original_cache_cls(Path(tmp) / "c.json")  # type: ignore[assignment]
            fake_config = _isolated(dict(app_config.load_config()), tmp)
            fake_config["provider"] = "fake"
            fake_result = pipeline_module.run_job([fixture], "zh", "fr", fake_config)
            job_dirs.append(pipeline_module.OUTPUT_DIR / fake_result.job_id)

            mock_config = dict(app_config.load_config())
            mock_config["provider"] = "mock"
            mock_result = pipeline_module.run_job([fixture], "zh", "fr", mock_config)
            job_dirs.append(pipeline_module.OUTPUT_DIR / mock_result.job_id)
    finally:
        pipeline_module.TranslationCache = original_cache_cls  # type: ignore[assignment]
        for job_dir in job_dirs:
            shutil.rmtree(job_dir, ignore_errors=True)

    stats = fake_result.files[0].stats
    check(stats.model == "fake-model", f"stats.model: {stats.model!r}")
    check(stats.prompt_version == "v1", f"stats.prompt_version: {stats.prompt_version!r}")
    check(fake_result.stats.model == "fake-model", "JobResult.stats lost the model")
    check("model=fake-model prompt=v1" in stats.summary(), f"summary: {stats.summary()}")

    mock_stats = mock_result.files[0].stats
    check(mock_stats.model is None, "mock stats.model should be None")
    check("model=" not in mock_stats.summary(), "mock summary must be unchanged")
    check(isinstance(get_translator({"provider": "mock"}), MockTranslator), "mock factory")
    check(
        JobStats(total_blocks=2, unique_blocks=2).summary()
        == "blocks=2 unique=2 (dedup 0%) cache_hits=0 api_calls=0 estimated_tokens=0",
        "JobStats.summary format changed",
    )


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()
    tests = [
        test_committed_config,
        test_invalid_values,
        test_defaults_without_section,
        test_gateway_request_carries_model_and_temperature,
        test_fake_and_anthropic_translators,
        test_stats_model_and_prompt_version,
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
