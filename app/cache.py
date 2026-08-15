"""Block-hash deduplication and session-persisted translation cache."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "tmp" / "cache.json"


def _hash_key(src: str, tgt: str, text: str) -> str:
    """Return sha256(src|tgt|text) as the cache key. Content itself is never logged."""
    payload = f"{src}|{tgt}|{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class TranslationCache:
    """A hash -> translated text cache, persisted as JSON."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_CACHE_PATH
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                self._data = json.load(handle)
        except (OSError, ValueError) as error:
            logger.warning("cache: failed to load %s (%s), starting empty", self._path.name, type(error).__name__)
            self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, ensure_ascii=False)

    def get_many(self, texts: list[str], src: str, tgt: str) -> dict[str, str]:
        """Return {text: cached_translation} for every text found in the cache."""
        hits: dict[str, str] = {}
        for text in texts:
            key = _hash_key(src, tgt, text)
            if key in self._data:
                hits[text] = self._data[key]
        return hits

    def set_many(self, translations: dict[str, str], src: str, tgt: str) -> None:
        """Store {text: translation} entries and persist to disk."""
        if not translations:
            return
        for text, translated in translations.items():
            key = _hash_key(src, tgt, text)
            self._data[key] = translated
        self._save()

    def purge(self) -> None:
        """Clear the cache, in memory and on disk."""
        self._data = {}
        if self._path.is_file():
            self._path.unlink()

    def __len__(self) -> int:
        return len(self._data)
