"""Translation memory: embedders, cached vector index, and a prompt context provider.

Two embedders are available: a deterministic hashing embedder (standard library
only, works offline) and an OpenAI-compatible gateway embedder. Segments of the
memory are embedded once and the vectors are cached on disk, keyed by the hash
of the memory file and the embedder id.

Logs carry counts and error types only: never segment text or tokens.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import auth
from app.llm.errors import TranslationError
from app.llm.retry import RETRYABLE_STATUS, RetryableError, call_with_retry

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = REPO_ROOT / "tmp" / "rag"

_WHITESPACE_RE = re.compile(r"\s+")


class Embedder(Protocol):
    """Turns texts into vectors. ``id`` is part of the vector-cache key."""

    id: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def normalise(text: str) -> str:
    """NFKC, casefold, collapse whitespace, strip."""
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


class HashingEmbedder:
    """Character n-gram feature hashing into a fixed number of buckets.

    Uses hashlib (never the built-in hash function, which is salted per process), so the
    vectors are identical across processes and runs.
    """

    def __init__(self, dims: int = 1024, ngram: tuple[int, int] = (1, 3)) -> None:
        if dims < 1 or ngram[0] < 1 or ngram[1] < ngram[0]:
            raise ValueError("invalid HashingEmbedder parameters")
        self._dims = dims
        self._ngram = ngram
        self.id = f"hashing-v1-{dims}-n{ngram[0]}-{ngram[1]}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        clean = normalise(text)
        vector = [0.0] * self._dims
        low, high = self._ngram
        for n in range(low, high + 1):
            for start in range(len(clean) - n + 1):
                digest = hashlib.blake2b(clean[start : start + n].encode("utf-8"), digest_size=8)
                value = int.from_bytes(digest.digest(), "big")
                bucket = value % self._dims
                sign = 1.0 if (value >> 63) & 1 else -1.0
                vector[bucket] += sign
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0.0:
            return vector
        return [x / norm for x in vector]


class GatewayEmbedder:
    """Embeddings from an OpenAI-compatible gateway (POST {base_url}/embeddings)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._client = client
        self.id = f"gateway:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = {"model": self._model, "input": texts}
        response = call_with_retry(lambda: self._post(body), label="embeddings")
        return self._parse(response, len(texts))

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {auth.get_token()}"}
        url = f"{self._base_url}/embeddings"
        try:
            if self._client is not None:
                response = self._client.post(url, json=body, headers=headers)
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as error:
            raise TranslationError(f"embeddings request failed: {type(error).__name__}") from None

        if response.status_code in RETRYABLE_STATUS:
            raise RetryableError(f"embeddings returned {response.status_code}")
        if response.status_code >= 400:
            # Status only: the body could echo the input text.
            raise TranslationError(f"embeddings returned {response.status_code}")
        return response

    @staticmethod
    def _parse(response: httpx.Response, expected: int) -> list[list[float]]:
        try:
            items = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors = [[float(x) for x in item["embedding"]] for item in items]
        except (KeyError, ValueError, TypeError, AttributeError):
            raise TranslationError("malformed embeddings response") from None
        if len(vectors) != expected:
            raise TranslationError("embeddings response has the wrong number of vectors")
        return vectors


class TMEntry(BaseModel):
    """One validated translation pair; provenance fields are kept as extras."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    src: str | None = None
    tgt: str | None = None


@dataclass(frozen=True)
class TMHit:
    entry: TMEntry
    score: float


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class TranslationMemory:
    """JSONL translation memory with a disk-cached vector index."""

    def __init__(
        self,
        path: str | Path,
        embedder: Embedder,
        *,
        default_src: str = "zh",
        default_tgt: str = "en",
        cache_dir: str | Path | None = None,
    ) -> None:
        self._path = Path(path)
        self._embedder = embedder
        self._default_src = default_src
        self._default_tgt = default_tgt
        self._cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
        self._entries: list[TMEntry] = []
        self._vectors: list[list[float]] = []
        self._load()

    def __len__(self) -> int:
        return len(self._entries)

    # -- loading ---------------------------------------------------------- #

    def _load(self) -> None:
        raw = self._path.read_bytes()
        self._entries = self._parse(raw.decode("utf-8-sig"))
        self._vectors = self._load_or_build_vectors(raw)

    def _parse(self, text: str) -> list[TMEntry]:
        entries: list[TMEntry] = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entries.append(TMEntry.model_validate(json.loads(line)))
            except (json.JSONDecodeError, ValidationError):
                raise ValueError(f"invalid translation memory entry at line {number}") from None
        return entries

    def _cache_path(self, raw: bytes) -> Path:
        digest = hashlib.sha256(raw).hexdigest()[:16]
        embedder_id = self._embedder.id.replace(":", "_").replace("/", "_")
        return self._cache_dir / f"tm-{digest}-{embedder_id}.json"

    def _load_or_build_vectors(self, raw: bytes) -> list[list[float]]:
        cache_path = self._cache_path(raw)
        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                vectors = data["vectors"]
                if data["embedder"] != self._embedder.id or len(vectors) != len(self._entries):
                    raise ValueError("stale cache")
                return [[float(x) for x in vector] for vector in vectors]
            except (OSError, ValueError, KeyError, TypeError) as error:
                logger.warning("translation memory cache rebuilt (%s)", type(error).__name__)
        vectors = self._embedder.embed([entry.source for entry in self._entries])
        self._write_cache(cache_path, vectors)
        logger.info("translation memory indexed: %d entries", len(vectors))
        return vectors

    def _write_cache(self, cache_path: Path, vectors: list[list[float]]) -> None:
        payload = {"embedder": self._embedder.id, "vectors": vectors}
        tmp_path = cache_path.with_name(cache_path.name + ".tmp")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp_path, cache_path)
        except OSError as error:
            logger.warning("translation memory cache not written (%s)", type(error).__name__)

    # -- search / add ----------------------------------------------------- #

    def _pair(self, entry: TMEntry) -> tuple[str, str]:
        return (entry.src or self._default_src, entry.tgt or self._default_tgt)

    def search(
        self, text: str, src: str, tgt: str, top_k: int = 3, min_score: float = 0.3
    ) -> list[TMHit]:
        """Best entries for ``text`` in the (src, tgt) pair, by cosine similarity."""
        if not self._entries or top_k < 1:
            return []
        query = self._embedder.embed([text])[0]
        hits = [
            TMHit(entry, score)
            for entry, vector in zip(self._entries, self._vectors, strict=True)
            if self._pair(entry) == (src, tgt) and (score := _dot(query, vector)) >= min_score
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.entry.id))
        return hits[:top_k]

    def add(
        self,
        source: str,
        reference: str,
        *,
        src: str,
        tgt: str,
        entry_id: str | None = None,
        **provenance: Any,
    ) -> TMEntry:
        """Append one pair to the file, then update the index and its cache."""
        if entry_id is None:
            existing = {entry.id for entry in self._entries}
            number = len(self._entries) + 1
            while f"tm-user-{number}" in existing:
                number += 1
            entry_id = f"tm-user-{number}"
        entry = TMEntry(
            id=entry_id, source=source, reference=reference, src=src, tgt=tgt, **provenance
        )
        raw = self._path.read_bytes()
        line = json.dumps(entry.model_dump(exclude_none=True), ensure_ascii=False)
        prefix = b"" if not raw or raw.endswith(b"\n") else b"\n"
        with self._path.open("ab") as handle:
            handle.write(prefix + line.encode("utf-8") + b"\n")

        vector = self._embedder.embed([entry.source])[0]
        self._entries.append(entry)
        self._vectors.append(vector)
        self._write_cache(self._cache_path(self._path.read_bytes()), self._vectors)
        return entry


class TranslationMemoryContextProvider:
    """Renders the best validated translations as a prompt block."""

    name = "translation_memory"

    def __init__(self, memory: TranslationMemory, top_k: int = 3, min_score: float = 0.3) -> None:
        self._memory = memory
        self._top_k = top_k
        self._min_score = min_score

    def render(self, segments: list[str], src: str, tgt: str) -> tuple[str, int]:
        """Return (block text, number of examples injected)."""
        best: dict[str, TMHit] = {}
        for segment in segments:
            for hit in self._memory.search(segment, src, tgt, self._top_k, self._min_score):
                known = best.get(hit.entry.id)
                if known is None or hit.score > known.score:
                    best[hit.entry.id] = hit
        hits = sorted(best.values(), key=lambda hit: (-hit.score, hit.entry.id))[: self._top_k]
        if not hits:
            return "", 0
        examples = [
            f"Source: {_one_line(hit.entry.source)}\nTranslation: {_one_line(hit.entry.reference)}"
            for hit in hits
        ]
        return "Examples of validated translations:\n" + "\n\n".join(examples), len(hits)

    def context_for(self, segments: list[str], src: str, tgt: str) -> str:
        return self.render(segments, src, tgt)[0]


def _one_line(text: str) -> str:
    """Keep each example on one line so it can never look like a '- x => y' pair."""
    return _WHITESPACE_RE.sub(" ", text).strip()
