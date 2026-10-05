"""Tests for app.rag.memory: embedders, cached index, TM context provider.

No network access; every cache dir and TM file lives in a temporary directory.
Run: python tests/test_rag_memory.py
"""

from __future__ import annotations

import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app import auth  # noqa: E402
from app.llm.errors import TranslationError  # noqa: E402
from app.llm.fake_provider import GLOSSARY_LINE_RE  # noqa: E402
from app.llm.retry import RetryableError  # noqa: E402
from app.rag import memory as memory_module  # noqa: E402
from app.rag.memory import (  # noqa: E402
    GatewayEmbedder,
    HashingEmbedder,
    TranslationMemory,
    TranslationMemoryContextProvider,
)

FAILURES: list[str] = []
SEED = ROOT / "data" / "tm" / "seed_zh_en.jsonl"


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def _seed_entries() -> list[dict]:
    return [json.loads(line) for line in SEED.read_text(encoding="utf-8").splitlines() if line]


class SpyEmbedder:
    """Wraps HashingEmbedder and counts embed calls."""

    def __init__(self) -> None:
        self._inner = HashingEmbedder()
        self.id = self._inner.id
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return self._inner.embed(texts)


# --------------------------------------------------------------------------- #
# Embedders
# --------------------------------------------------------------------------- #


def test_hashing_deterministic() -> None:
    """Same vectors across instances and across a fresh process; unit norm."""
    texts = ["本公司宣派末期股息", "Final  DIVIDEND ｆｕｌｌ"]
    first = HashingEmbedder().embed(texts)
    second = HashingEmbedder().embed(texts)
    check(first == second, "vectors differ across instances")
    for vector in first:
        check(abs(math.sqrt(sum(x * x for x in vector)) - 1.0) < 1e-9, "norm is not 1")
    check(HashingEmbedder().embed([""])[0] == [0.0] * 1024, "empty text is not a zero vector")
    check(HashingEmbedder().id == "hashing-v1-1024-n1-3", "unexpected embedder id")

    code = (
        "import json,sys;sys.path.insert(0,sys.argv[1]);"
        "from app.rag.memory import HashingEmbedder;"
        "print(json.dumps(HashingEmbedder().embed(json.loads(sys.argv[2]))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(ROOT), json.dumps(texts)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PYTHONHASHSEED": "random", "SYSTEMROOT": "C:\\Windows"},
        check=False,
    )
    check(result.returncode == 0, f"subprocess failed: {result.stderr[-200:]}")
    if result.returncode == 0:
        other = json.loads(result.stdout)
        close = all(
            abs(a - b) < 1e-12
            for va, vb in zip(first, other, strict=True)
            for a, b in zip(va, vb, strict=True)
        )
        check(close, "vectors differ across processes")


def test_gateway_embedder() -> None:
    """Request shape, ordering by index, and error mapping (no network)."""
    seen: list[httpx.Request] = []
    original = auth.get_token
    auth.get_token = lambda: "tok"  # type: ignore[assignment]
    try:

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ]
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        embedder = GatewayEmbedder("http://gw.test/v1/", "emb-model", client=client)
        vectors = embedder.embed(["a", "b"])
        check(vectors == [[1.0, 0.0], [0.0, 1.0]], f"vectors not ordered by index: {vectors}")
        check(embedder.id == "gateway:emb-model", "wrong gateway embedder id")
        request = seen[0]
        check(str(request.url) == "http://gw.test/v1/embeddings", f"url: {request.url}")
        check(request.headers["authorization"] == "Bearer tok", "missing bearer token")
        check(
            json.loads(request.content) == {"model": "emb-model", "input": ["a", "b"]},
            "wrong request body",
        )

        def client_for(response: httpx.Response) -> httpx.Client:
            return httpx.Client(transport=httpx.MockTransport(lambda _request: response))

        echoed = "SECRET-INPUT-TEXT"
        try:
            GatewayEmbedder(
                "http://gw.test",
                "m",
                client=client_for(httpx.Response(400, text=f"bad input {echoed}")),
            ).embed([echoed])
            check(False, "HTTP 400 did not raise")
        except TranslationError as error:
            check("400" in str(error), "status code missing from error")
            check(echoed not in str(error), "error echoes the response body")

        try:
            GatewayEmbedder(
                "http://gw.test", "m", client=client_for(httpx.Response(200, json={"oops": 1}))
            ).embed(["x"])
            check(False, "malformed response did not raise")
        except TranslationError:
            pass

        try:
            GatewayEmbedder(
                "http://gw.test", "m", client=client_for(httpx.Response(200, json={"data": []}))
            ).embed(["x"])
            check(False, "wrong vector count did not raise")
        except TranslationError:
            pass

        # 503 is retried then mapped to TranslationError (no real sleeping).
        import app.llm.retry as retry_module

        original_sleep = retry_module.time.sleep
        retry_module.time.sleep = lambda _seconds: None  # type: ignore[assignment]
        try:
            calls: list[int] = []

            def always_503(_request: httpx.Request) -> httpx.Response:
                calls.append(1)
                return httpx.Response(503)

            flaky = httpx.Client(transport=httpx.MockTransport(always_503))
            try:
                GatewayEmbedder("http://gw.test", "m", client=flaky).embed(["x"])
                check(False, "503 did not raise")
            except TranslationError:
                check(len(calls) == retry_module.MAX_RETRIES, f"retries: {len(calls)}")
        finally:
            retry_module.time.sleep = original_sleep  # type: ignore[assignment]
        check(issubclass(RetryableError, Exception), "RetryableError import sanity")
    finally:
        auth.get_token = original  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Translation memory
# --------------------------------------------------------------------------- #


def test_seed_search() -> None:
    """Near duplicate ranks first; top_k, threshold and pair filter are honoured."""
    entries = _seed_entries()
    check(len(entries) == 32, f"seed has {len(entries)} entries")
    target = entries[5]
    near = target["source"].replace(target["source"][3], "X", 1)
    with tempfile.TemporaryDirectory() as cache:
        memory = TranslationMemory(SEED, HashingEmbedder(), cache_dir=cache)
        check(len(memory) == 32, "wrong memory size")
        hits = memory.search(near, "zh", "en", top_k=3, min_score=0.0)
        check(len(hits) == 3, f"top_k not honoured: {len(hits)}")
        check(hits[0].entry.id == target["id"], "near duplicate does not rank first")
        scores = [hit.score for hit in hits]
        check(scores == sorted(scores, reverse=True), "hits not sorted by score")
        check(len(memory.search(near, "zh", "en", top_k=1, min_score=0.0)) == 1, "top_k=1")

        high = memory.search(near, "zh", "en", top_k=10, min_score=0.99)
        check(high == [] or all(hit.score >= 0.99 for hit in high), "min_score not applied")
        check(
            len(memory.search(near, "zh", "en", top_k=32, min_score=0.5))
            < len(memory.search(near, "zh", "en", top_k=32, min_score=-1.0)),
            "threshold does not filter",
        )
        check(memory.search(near, "zh", "fr", top_k=3, min_score=-1.0) == [], "zh->fr not empty")
        check(memory.search(near, "en", "zh", top_k=3, min_score=-1.0) == [], "en->zh not empty")
        kept = hits[0].entry.model_dump()
        check("issuer" in kept and "source_url_zh" in kept, "provenance fields dropped")


def test_vector_cache() -> None:
    """Cache file created, reused, invalidated on change, rebuilt when corrupt."""
    with tempfile.TemporaryDirectory() as tmp:
        tm_path = Path(tmp) / "tm.jsonl"
        shutil.copyfile(SEED, tm_path)
        cache_dir = Path(tmp) / "cache"

        spy = SpyEmbedder()
        TranslationMemory(tm_path, spy, cache_dir=cache_dir)
        files = list(cache_dir.glob("tm-*.json"))
        check(len(files) == 1, f"cache files: {files}")
        check(spy.calls == 1, "first load should embed once")
        check(files[0].name.endswith("-hashing-v1-1024-n1-3.json"), f"name: {files[0].name}")

        spy2 = SpyEmbedder()
        TranslationMemory(tm_path, spy2, cache_dir=cache_dir)
        check(spy2.calls == 0, "second load did not reuse the cache")

        with tm_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": "x", "source": "新", "reference": "new"}) + "\n")
        spy3 = SpyEmbedder()
        memory = TranslationMemory(tm_path, spy3, cache_dir=cache_dir)
        check(spy3.calls == 1 and len(memory) == 33, "file change did not invalidate the cache")
        check(len(list(cache_dir.glob("tm-*.json"))) == 2, "new cache file missing")

        newest = max(cache_dir.glob("tm-*.json"), key=lambda path: path.stat().st_mtime_ns)
        newest.write_text("{not json", encoding="utf-8")
        spy4 = SpyEmbedder()
        TranslationMemory(tm_path, spy4, cache_dir=cache_dir)
        check(spy4.calls == 1, "corrupt cache was not rebuilt")
        json.loads(newest.read_text(encoding="utf-8"))

        bad = Path(tmp) / "bad.jsonl"
        bad.write_text('{"id":"a","source":"s","reference":"r"}\n\nnot json\n', encoding="utf-8")
        try:
            TranslationMemory(bad, HashingEmbedder(), cache_dir=cache_dir)
            check(False, "bad line did not raise")
        except ValueError as error:
            check("line 3" in str(error), f"line number missing: {error}")
            check("not json" not in str(error), "error echoes file content")


def test_add() -> None:
    """add() persists to the file and the index, and survives a reload."""
    with tempfile.TemporaryDirectory() as tmp:
        tm_path = Path(tmp) / "tm.jsonl"
        tm_path.write_text("", encoding="utf-8")
        cache_dir = Path(tmp) / "cache"
        memory = TranslationMemory(tm_path, HashingEmbedder(), cache_dir=cache_dir)
        check(len(memory) == 0 and memory.search("x", "zh", "en") == [], "empty memory")

        entry = memory.add(
            "本公司宣派末期股息", "The Company declares a final dividend", src="zh", tgt="en"
        )
        check(entry.id == "tm-user-1", f"id: {entry.id}")
        memory.add("你好世界", "Bonjour le monde", src="zh", tgt="fr", entry_id="custom", note="n")
        check(len(memory) == 2, "memory size after add")

        hits = memory.search("本公司宣派末期股息", "zh", "en")
        check(bool(hits) and hits[0].entry.id == "tm-user-1", "added entry not searchable")
        en_ids = [hit.entry.id for hit in memory.search("你好世界", "zh", "en", min_score=-1.0)]
        check("custom" not in en_ids, "pair filter after add")
        check(memory.search("你好世界", "zh", "fr")[0].entry.id == "custom", "fr entry missing")

        lines = tm_path.read_text(encoding="utf-8").splitlines()
        check(len(lines) == 2 and "本公司" in lines[0], "file not appended as UTF-8 JSON lines")

        spy = SpyEmbedder()
        reloaded = TranslationMemory(tm_path, spy, cache_dir=cache_dir)
        check(spy.calls == 0 and len(reloaded) == 2, "cache not rewritten after add")


def test_context_provider() -> None:
    """Block format, empty case, merge/top_k, and no glossary-looking line."""
    with tempfile.TemporaryDirectory() as cache:
        memory = TranslationMemory(SEED, HashingEmbedder(), cache_dir=cache)
        entries = _seed_entries()
        provider = TranslationMemoryContextProvider(memory, top_k=3, min_score=0.0)
        check(provider.name == "translation_memory", "wrong provider name")

        segments = [entries[0]["source"], entries[1]["source"], entries[0]["source"]]
        text, count = provider.render(segments, "zh", "en")
        check(count == 3, f"count: {count}")
        check(text.startswith("Examples of validated translations:\n"), "wrong header")
        check(text.count("Source: ") == 3 and text.count("Translation: ") == 3, "block shape")
        check(entries[0]["reference"] in text, "reference missing from block")
        check(
            re.search(GLOSSARY_LINE_RE, text, re.MULTILINE) is None,
            "TM block matches the glossary line format",
        )
        check(provider.context_for(segments, "zh", "en") == text, "context_for differs")

        check(provider.render(segments, "zh", "fr") == ("", 0), "zh->fr should be empty")
        strict = TranslationMemoryContextProvider(memory, top_k=3, min_score=1.01)
        check(strict.render(segments, "zh", "en") == ("", 0), "no-hit case not empty")


def test_leave_one_out_scores() -> None:
    """Leave-one-out top-1 scores over the seed (printed for threshold tuning)."""
    entries = _seed_entries()
    embedder = HashingEmbedder()
    vectors = embedder.embed([entry["source"] for entry in entries])
    tops = []
    for i, vector in enumerate(vectors):
        others = [
            sum(x * y for x, y in zip(vector, other, strict=True))
            for j, other in enumerate(vectors)
            if j != i
        ]
        tops.append(max(others))
    print(
        "leave-one-out top-1: "
        f"min={min(tops):.3f} median={statistics.median(tops):.3f} max={max(tops):.3f}"
    )
    check(all(-1.0 <= score <= 1.0 + 1e-9 for score in tops), "score out of range")


def test_no_builtin_hash() -> None:
    """The embedder source must use hashlib, never the salted built-in hash()."""
    source = Path(memory_module.__file__).read_text(encoding="utf-8")
    check("hashlib.blake2b" in source, "hashlib.blake2b not used")
    check(re.search(r"(?<![\w.])hash\(", source) is None, "built-in hash() used")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        try:
            test()
        except Exception as error:  # noqa: BLE001
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
    print(f"{len(tests)} tests, {len(FAILURES)} failures")
    for failure in FAILURES:
        print(f"  FAIL: {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
