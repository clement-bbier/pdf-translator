"""Translation suite: 3 configurations x N runs over the zh->en HKEX dataset.

Configurations: ``no_rag``, ``glossary`` and ``glossary_tm`` (retrieval-augmented
context providers switched on in a copy of the loaded config). Providers are
built directly, never through ``build_provider``: no cache (it would hide run
to run variance) and no tracing (no document text is written to ``traces/``).

``run()`` follows the layout suite protocol (flat ``dict[str, float]``) and uses
the offline fake provider. ``evaluate()`` is the full API. Reports carry numbers
only, never segment text.

The suite builds translators exactly as the application does (versioned
prompts, RAG context from app.rag, verification from app.verification).
"""

from __future__ import annotations

import copy
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import load_config
from app.llm.base import LLMProvider, LLMRequest, LLMResponse
from app.llm.fake_provider import FakeProvider
from app.llm.translator import LLMTranslator
from evaluation import metrics as m
from evaluation.translation_dataset import (
    DATASET_PATH,
    DEFAULT_GLOSSARY_PATH,
    ROOT,
    GlossaryEntry,
    load_glossary,
    load_segments,
)

NAME = "translation"
SRC = "zh"
TGT = "en"
RUNS = 3
CONFIGURATIONS: dict[str, dict[str, bool]] = {
    "no_rag": {"glossary": False, "translation_memory": False},
    "glossary": {"glossary": True, "translation_memory": False},
    "glossary_tm": {"glossary": True, "translation_memory": True},
}
METRIC_NAMES = (
    "glossary_adherence",
    "check_pass_rate",
    "similarity",
    "chrf",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "latency_p95_ms",
)
_HIGHER_IS_BETTER = {"glossary_adherence", "check_pass_rate", "similarity", "chrf"}
_LOWER_IS_BETTER = {"cost_usd", "input_tokens", "output_tokens"}
FAKE_WARNING = (
    "**Fake model: these numbers measure the pipeline plumbing, not translation quality.**"
)


DEFAULT_TM_PATH = "data/tm/seed_zh_en.jsonl"


class SuiteUnavailable(RuntimeError):
    """The suite cannot run in this environment (for example a missing API key)."""


# --------------------------------------------------------------------------- adapters


def _llm_settings(config: dict[str, Any]) -> tuple[Any, str | None]:
    """Validated llm settings (app.config)."""
    from app.config import llm_settings

    return llm_settings(config), None


def _rag_context(variant_config: dict[str, Any]) -> Any:
    """RagContext for the configuration's rag switches (app.rag); None when both are off."""
    from app.rag.context import build_rag_context

    return build_rag_context(variant_config)


def _build_translator(
    provider: LLMProvider,
    model: str,
    temperature: float | None,
    settings: Any,
    variant_config: dict[str, Any],
) -> tuple[LLMTranslator, list[str]]:
    """LLMTranslator as the application builds it: versioned prompts, RAG context,
    and verification (retry once, then flag) as configured in config.yaml."""
    from app.llm.prompts import PromptBuilder
    from app.verification.settings import verification_config

    translator = LLMTranslator(
        provider,
        model=model,
        temperature=temperature,
        max_tokens=settings.max_tokens,
        prompt_builder=PromptBuilder(settings.prompt_version),
        context_provider=_rag_context(variant_config),
    )
    verification = verification_config(variant_config)
    if verification.enabled:
        translator.enable_verification(verification)
    return translator, []


def _checks(
    source: str, output: str, translated: bool = True
) -> tuple[dict[str, bool], str | None]:
    """Verification checks of app.verification on one segment's final output.

    Glossary terms are not required here: glossary_adherence measures them
    separately, and the no_rag configuration injects none.
    """
    from app.verification import run_checks

    if not translated:
        return {"translated": False}, None
    report = run_checks({"0": source}, json.dumps({"0": output}, ensure_ascii=False), SRC, TGT)
    failed = {finding.check for finding in report.findings}
    checks = {
        name: name not in failed for name in ("json_valid", "segment_count", "numbers", "dates")
    }
    checks["translated"] = True
    return checks, None


# ------------------------------------------------------------------------- machinery


class MeteredProvider:
    """Wraps a provider and records model, tokens, duration and success per call."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.records: list[dict[str, Any]] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        start = time.perf_counter()
        try:
            response = self._inner.complete(request)
        except Exception:
            self.records.append(
                {
                    "model": request.model,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "duration_ms": (time.perf_counter() - start) * 1000.0,
                    "ok": False,
                }
            )
            raise
        self.records.append(
            {
                "model": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "duration_ms": (time.perf_counter() - start) * 1000.0,
                "ok": True,
            }
        )
        return response


@dataclass
class TranslationRun:
    """Result of one evaluate() call."""

    provider: str
    models: list[tuple[str, float | None]]
    prompt_version: str
    dataset_size: int
    runs: int
    generated_at: str
    notes: list[str] = field(default_factory=list)
    raw: dict[str, list[float]] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    failed_calls: int = 0
    dataset_name: str = ""

    def _prefix(self, model: str, config: str) -> str:
        return config if self.provider == "fake" else f"{model}.{config}"

    def render_markdown(self) -> str:
        """Human-readable report; numbers only, never segment text."""
        models = ", ".join(
            f"{name} (temperature {'not sent' if temp is None else temp})"
            for name, temp in self.models
        )
        lines = [
            "# Translation evaluation",
            "",
            f"- Provider: {self.provider}",
            f"- Model(s): {models}",
            f"- Prompt version: {self.prompt_version}",
            f"- Dataset: {self.dataset_name} ({self.dataset_size} segments)",
            f"- Runs: {self.runs}",
            "- Cache: disabled",
            f"- Failed segment calls: {self.failed_calls}",
            f"- Date (UTC): {self.generated_at}",
            "",
        ]
        if self.provider == "fake":
            lines += [FAKE_WARNING, ""]
        if self.notes:
            lines += ["## Notes", ""] + [f"- {note}" for note in self.notes] + [""]
        for metric in METRIC_NAMES:
            lines += [
                f"## {metric}",
                "",
                "| Model | " + " | ".join(CONFIGURATIONS) + " |",
                "| --- | " + " | ".join("---:" for _ in CONFIGURATIONS) + " |",
            ]
            for model, _temp in self.models:
                cells = []
                for config in CONFIGURATIONS:
                    key = f"{self._prefix(model, config)}.{metric}"
                    if f"{key}.mean" in self.metrics:
                        digits = 6 if metric == "cost_usd" else 3
                        cells.append(
                            f"{self.metrics[f'{key}.mean']:.{digits}f} ± "
                            f"{self.metrics[f'{key}.std']:.{digits}f}"
                        )
                    else:
                        cells.append("-")
                lines.append(f"| {model} | " + " | ".join(cells) + " |")
            lines.append("")
        return "\n".join(lines)


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _glossary_path(config: dict[str, Any]) -> Path:
    """rag.glossary_path when that file exists, else the finance glossary."""
    configured = (config.get("rag") or {}).get("glossary_path")
    if configured and _resolve(configured).is_file():
        return _resolve(configured)
    return DEFAULT_GLOSSARY_PATH


def _variant_config(config: dict[str, Any], flags: dict[str, bool]) -> dict[str, Any]:
    variant = copy.deepcopy(config)
    rag = variant.get("rag")
    if not isinstance(rag, dict):
        rag = {}
        variant["rag"] = rag
    rag["glossary"] = flags["glossary"]
    rag["translation_memory"] = flags["translation_memory"]
    rag["glossary_path"] = str(_glossary_path(config))
    return variant


def _models_for(provider: str, settings: Any) -> list[tuple[str, float | None]]:
    if provider == "fake":
        return [(FakeProvider.MODEL_ID, settings.temperature)]
    return [(settings.model, settings.temperature)] + [
        (c.model, c.temperature) for c in settings.compare_models
    ]


def _price(settings: Any, model: str) -> Any:
    prices = settings.prices
    return prices.get(model) if hasattr(prices, "get") else None


def _run_once(
    translator: LLMTranslator,
    metered: MeteredProvider,
    segments: list[Any],
    entries: list[GlossaryEntry],
    settings: Any,
    notes: list[str],
) -> tuple[dict[str, float], int]:
    pairs: list[tuple[str, str]] = []
    sims: list[float] = []
    chrfs: list[float] = []
    passes: list[bool] = []
    latencies: list[float] = []
    cost = 0.0
    failed = 0
    for seg in segments:
        before = len(metered.records)
        translated = True
        try:
            output = translator.translate_blocks([seg.source], SRC, TGT)[0]
        except Exception:  # noqa: BLE001 - a bad segment is a warning, never raised
            output = ""
            translated = False
            failed += 1
        new = metered.records[before:]
        latencies.append(sum(r["duration_ms"] for r in new))
        for rec in new:
            price = _price(settings, rec["model"])
            if price is None:
                notes.append(f"no price for {rec['model']}")
            else:
                cost += m.cost_usd(rec["input_tokens"], rec["output_tokens"], price) or 0.0
        checks, note = _checks(seg.source, output, translated)
        if note:
            notes.append(note)
        passes.append(all(checks.values()))
        pairs.append((seg.source, output))
        sims.append(m.similarity(output, seg.reference))
        chrfs.append(m.chrf(output, seg.reference))
    values: dict[str, float] = {
        "check_pass_rate": sum(passes) / len(passes),
        "similarity": sum(sims) / len(sims),
        "chrf": sum(chrfs) / len(chrfs),
        "cost_usd": round(cost, 6),
        "input_tokens": float(sum(r["input_tokens"] for r in metered.records)),
        "output_tokens": float(sum(r["output_tokens"] for r in metered.records)),
        "latency_ms": sum(latencies) / len(latencies),
        "latency_p95_ms": m.p95(latencies),
    }
    adherence = m.glossary_adherence(pairs, entries, TGT)
    if adherence is not None:
        values["glossary_adherence"] = adherence
    return values, failed


def _base_provider(
    provider: str, settings: Any, provider_factory: Callable[[], LLMProvider] | None
) -> LLMProvider:
    if provider not in ("fake", "anthropic"):
        raise ValueError(f"unsupported provider for the translation suite: {provider!r}")
    if provider_factory is not None:
        return provider_factory()
    if provider == "fake":
        return FakeProvider()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SuiteUnavailable("ANTHROPIC_API_KEY is not set")
    from app.llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider(timeout=settings.timeout)


def evaluate(
    provider: str = "fake",
    *,
    runs: int = RUNS,
    limit: int | None = None,
    config: dict[str, Any] | None = None,
    provider_factory: Callable[[], LLMProvider] | None = None,
    dataset_path: Path = DATASET_PATH,
) -> TranslationRun:
    """Run every model x configuration x run and aggregate with mean/std."""
    config = config if config is not None else load_config()
    notes: list[str] = []

    def add(note: str | None) -> None:
        if note and note not in notes:
            notes.append(note)

    settings, note = _llm_settings(config)
    add(note)
    base = _base_provider(provider, settings, provider_factory)
    full_default = limit is None and dataset_path == DATASET_PATH
    segments = load_segments(dataset_path, expected=30 if full_default else None)
    if limit is not None:
        segments = segments[:limit]
    entries = load_glossary(_glossary_path(config))
    models = _models_for(provider, settings)

    result = TranslationRun(
        provider=provider,
        models=models,
        prompt_version=settings.prompt_version,
        dataset_size=len(segments),
        runs=runs,
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        dataset_name=Path(dataset_path).name,
    )
    for model, temperature in models:
        for cfg_name, flags in CONFIGURATIONS.items():
            variant = _variant_config(config, flags)
            prefix = result._prefix(model, cfg_name)
            for _ in range(runs):
                metered = MeteredProvider(base)
                translator, t_notes = _build_translator(
                    metered, model, temperature, settings, variant
                )
                for t_note in t_notes:
                    add(t_note)
                run_notes: list[str] = []
                values, failed = _run_once(
                    translator, metered, segments, entries, settings, run_notes
                )
                for r_note in run_notes:
                    add(r_note)
                result.failed_calls += failed
                for metric, value in values.items():
                    result.raw.setdefault(f"{prefix}.{metric}", []).append(value)
    for key, values in result.raw.items():
        mean, std = m.mean_std(values)
        result.metrics[f"{key}.mean"] = mean
        result.metrics[f"{key}.std"] = std
    result.notes = notes
    return result


def run() -> dict[str, float]:
    """Layout-suite protocol: flat metrics of the offline fake-model evaluation."""
    return evaluate("fake").metrics


# ---------------------------------------------------------------------- run.py hooks


def _metric_name(metric: str) -> str:
    parts = metric.rsplit(".", 2)
    return parts[-2] if len(parts) >= 2 else metric


def metric_direction(metric: str) -> bool | None:
    """True = higher is better, False = lower is better, None = informational."""
    if metric.endswith(".std"):
        return None
    name = _metric_name(metric)
    if name.startswith("latency"):
        return None
    if name in _HIGHER_IS_BETTER:
        return True
    if name in _LOWER_IS_BETTER:
        return False
    return None


def metric_tolerance(
    suite_key: str, metric: str, current: dict[str, float], baseline: dict[str, float]
) -> float:
    """Absolute tolerance: 0 offline; twice the larger std for real-model runs.

    ``current`` and ``baseline`` are the flat metric dicts of the suite key.
    """
    if suite_key != "translation@anthropic":
        return 0.0
    if not metric.endswith(".mean"):
        return 0.0
    std_key = metric[: -len(".mean")] + ".std"
    return 2.0 * max(current.get(std_key, 0.0), baseline.get(std_key, 0.0))
