"""Static, offline invariants of the container packaging and CI workflows.

No docker call, no network. Run: python tests/test_container.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _workflow(name: str) -> dict:
    return yaml.safe_load(_read(f".github/workflows/{name}"))


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_entries(text: str) -> dict[str, list[str]]:
    """Map pinned name -> list of hash lines for each requirement of the lock."""
    entries: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", line)
        if match:
            current = f"{_normalize(match.group(1))}=={match.group(2)}"
            entries[current] = []
        elif current and "--hash=sha256:" in line:
            entries[current].append(line.strip())
    return entries


def test_lock_matches_requirements() -> None:
    entries = _lock_entries(_read("requirements.lock"))
    check(bool(entries), "requirements.lock has no entries")
    for line in _read("requirements.txt").splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)==(\S+)", line.strip())
        if match:
            key = f"{_normalize(match.group(1))}=={match.group(2)}"
            check(key in entries, f"{key} missing from requirements.lock")
    for key, hashes in entries.items():
        check(len(hashes) >= 1, f"lock entry {key} has no hash")
    check("uv" not in {k.split("==")[0] for k in entries}, "uv must not be in the lock")
    check("uv==" not in _read("requirements.txt"), "uv must not be in requirements.txt")
    check("uv==" in _read("requirements-dev.txt"), "uv is not pinned in requirements-dev.txt")


def test_dockerfile() -> None:
    text = _read("Dockerfile")
    froms = re.findall(r"^FROM\s+(\S+)", text, flags=re.MULTILINE)
    check(len(froms) == 2, f"expected 2 FROM lines, got {len(froms)}")
    check(all(f == "${PYTHON_IMAGE}" for f in froms), "FROM must use the PYTHON_IMAGE arg")
    arg = re.search(r"^ARG PYTHON_IMAGE=(\S+)", text, flags=re.MULTILINE)
    check(arg is not None, "ARG PYTHON_IMAGE missing")
    if arg:
        check(re.search(r"3\.12\.\d+", arg.group(1)) is not None, "base tag is not patch-pinned")
    check("latest" not in text, "Dockerfile must not use latest")
    check("--require-hashes" in text, "pip install must use --require-hashes")
    check("--no-install-recommends" in text, "apt-get must use --no-install-recommends")
    check("rm -rf /var/lib/apt/lists" in text, "apt lists must be removed")
    check("fonts-noto-cjk" in text, "fonts-noto-cjk not installed")
    check("EXPOSE 8000" in text, "EXPOSE 8000 missing")
    check(
        re.search(r"^HEALTHCHECK\b[^\n]*(\\\n[^\n]*)*/healthz", text, flags=re.MULTILINE)
        is not None,
        "HEALTHCHECK on /healthz missing",
    )
    users = re.findall(r"^USER\s+(\S+)", text, flags=re.MULTILINE)
    check(bool(users), "no USER instruction")
    if users:
        check(users[-1].split(":")[0] not in {"root", "0"}, "final USER must not be root")
        check(users[-1].split(":")[0].isdigit(), "final USER should be numeric")
    check("--workers" not in text, "the job registry is in memory: one worker only")


def test_dockerignore() -> None:
    lines = [ln.strip() for ln in _read(".dockerignore").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("#")]
    check(bool(lines) and lines[0] == "*", ".dockerignore must start with the * allow-list")
    allowed = [ln[1:] for ln in lines if ln.startswith("!")]
    for forbidden in (".env", ".git", ".venv", "tests", "traces", ".claude"):
        check(
            not any(a.startswith(forbidden) for a in allowed),
            f".dockerignore allow-lists {forbidden}",
        )
    for needed in ("app/", "config.yaml", "requirements.lock"):
        check(needed in allowed, f"{needed} not allow-listed")


def test_compose() -> None:
    data = yaml.safe_load(_read("docker-compose.yml"))
    web = data["services"]["web"]
    check("127.0.0.1:8000:8000" in web["ports"], "compose must bind 127.0.0.1:8000:8000")
    env_file = web["env_file"]
    check(
        any(
            isinstance(e, dict) and e.get("path") == ".env" and e.get("required") is False
            for e in env_file
        ),
        "env_file .env must be optional (required: false)",
    )
    check("APP_PASSWORD" in web["environment"], "compose must pass APP_PASSWORD")
    check(web.get("cap_drop") == ["ALL"], "compose must drop all capabilities")
    check(web.get("read_only") is True, "compose root fs must be read-only")
    for volume in ("output", "tmp", "traces"):
        check(volume in data["volumes"], f"named volume {volume} missing")


def test_ci_workflow() -> None:
    text = _read(".github/workflows/ci.yml")
    jobs = _workflow("ci.yml")["jobs"]
    for job in ("checks", "docker", "eval"):
        check(job in jobs, f"ci.yml job {job} missing")
    check("secrets." not in text, "ci.yml must not reference any secret")
    _check_actions_pinned(text, "ci.yml")


def _check_actions_pinned(text: str, name: str) -> None:
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, flags=re.MULTILINE)
    for use in uses:
        ref = use.split("@", 1)[1] if "@" in use else ""
        check(bool(ref) and ref not in {"main", "master"}, f"{name}: unpinned action {use}")


def test_eval_real_workflow() -> None:
    text = _read(".github/workflows/eval-real.yml")
    data = yaml.safe_load(text)
    triggers = data.get("on", data.get(True))
    check(set(triggers) == {"workflow_dispatch"}, "eval-real must be manual only")
    check(data["permissions"] == {"contents": "read"}, "eval-real permissions must be read-only")
    refs = re.findall(r"secrets\.([A-Za-z0-9_]+)", text)
    check(
        bool(refs) and set(refs) == {"ANTHROPIC_API_KEY"}, "only secrets.ANTHROPIC_API_KEY allowed"
    )
    check(
        not re.search(r"\becho\b[^\n]*(ANTHROPIC_API_KEY|secrets\.)", text),
        "the secret must never be echoed",
    )
    check(
        not re.search(r"^\s*run:.*secrets\.", text, flags=re.MULTILINE),
        "the secret must not be interpolated in a run script",
    )
    _check_actions_pinned(text, "eval-real.yml")
    steps = [s for job in data["jobs"].values() for s in job["steps"]]
    key_steps = [s for s in steps if "ANTHROPIC_API_KEY" in str(s.get("env", {}))]
    check(len(key_steps) == 1, "the secret must be passed to exactly one step")
    check(
        any(s.get("if") == "always()" and "upload-artifact" in s.get("uses", "") for s in steps),
        "reports must be uploaded even on failure",
    )


def test_makefile_targets() -> None:
    text = _read("Makefile")
    for target in ("lock", "docker-build", "docker-up"):
        check(re.search(rf"^{target}:", text, flags=re.MULTILINE) is not None, f"target {target}")
        check(target in text.split("help:")[0], f"{target} not in .PHONY")


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        try:
            func()
        except Exception as error:  # noqa: BLE001
            FAILURES.append(f"{name} raised {type(error).__name__}: {error}")
    for failure in FAILURES:
        print(f"FAIL: {failure}")
    print(f"{len(tests)} tests, {len(FAILURES)} failures")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
