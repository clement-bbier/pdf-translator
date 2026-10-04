"""Secret and forbidden-term scanner, used before every commit.

Three entry points share the same rules:

- PreToolUse hook (Bash|PowerShell), no arguments: when the command is a
  ``git commit``, scan the lines that commit would add; block (exit 2) on a hit.
- ``--staged``: scan the staged diff (installed as a git pre-commit hook by
  ``scripts/install_git_hooks.py``, so commits made outside Claude Code are
  checked too).
- ``--all``: scan every tracked file (CI and the security-auditor agent).

Forbidden terms (names of employers, clients, internal hosts...) are read from
``.claude/forbidden-terms.local.txt``: one term per line, ``#`` for comments.
That file is gitignored and must never be committed; when it is absent only the
secret patterns apply. Matches are reported by term number, never by echoing
the term itself, so the list does not leak into transcripts or CI logs.

A line ending with ``secret-scan: allow`` is skipped (for test fixtures that
must contain a fake credential).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ALLOW, BLOCK, project_dir, read_input  # noqa: E402

FORBIDDEN_TERMS_FILE = Path(".claude") / "forbidden-terms.local.txt"
ALLOW_PRAGMA = "secret-scan: allow"

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "Anthropic API key": re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    "OpenAI-style API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "credential assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|passw(?:or)?d|access[_-]?token|client[_-]?secret)\b"
        r"\s*[:=]\s*['\"][A-Za-z0-9+/=_\-]{16,}['\"]"
    ),
}

# Never scanned: binary fixtures and the scanner's own pattern definitions.
_SKIP_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".ttf", ".otf", ".woff", ".zip"}


@dataclass(slots=True)
class Finding:
    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


def load_forbidden_terms(root: Path) -> list[str]:
    """Read the local, untracked list of forbidden terms (lower-cased)."""
    path = root / FORBIDDEN_TERMS_FILE
    if not path.is_file():
        return []
    terms = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        term = raw.strip()
        if term and not term.startswith("#"):
            terms.append(term.lower())
    return terms


def scan_line(text: str, terms: list[str]) -> list[str]:
    """Return the kinds of problems found on one line."""
    if text.rstrip().endswith(ALLOW_PRAGMA):
        return []
    kinds = [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(text)]
    lowered = text.lower()
    for index, term in enumerate(terms, start=1):
        if re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", lowered):
            kinds.append(f"forbidden term #{index} from {FORBIDDEN_TERMS_FILE.as_posix()}")
    return kinds


def scan_diff(diff: str, terms: list[str]) -> list[Finding]:
    """Scan the added lines of a unified diff."""
    findings: list[Finding] = []
    path = ""
    line_no = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else raw[4:]
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            line_no = int(match.group(1)) if match else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            if Path(path).suffix.lower() not in _SKIP_SUFFIXES and not _is_self(path):
                findings.extend(Finding(path, line_no, kind) for kind in scan_line(raw[1:], terms))
            line_no += 1
        elif not raw.startswith("-"):
            line_no += 1
    return findings


def _is_self(path: str) -> bool:
    return path.replace("\\", "/").endswith(".claude/hooks/secret_scan.py")


def _git(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return completed.stdout


def scan_tracked_files(root: Path, terms: list[str]) -> list[Finding]:
    """Scan every tracked file of the repository at ``root``."""
    findings: list[Finding] = []
    for rel in _git(["ls-files"], root).splitlines():
        path = root / rel
        if path.suffix.lower() in _SKIP_SUFFIXES or _is_self(rel) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            findings.extend(Finding(rel, number, kind) for kind in scan_line(line, terms))
    # A committed forbidden-terms list would leak the very terms it protects.
    if FORBIDDEN_TERMS_FILE.as_posix() in _git(["ls-files"], root).splitlines():
        findings.append(Finding(FORBIDDEN_TERMS_FILE.as_posix(), 0, "local term list is tracked"))
    return findings


_COMMIT_RE = re.compile(r"(?:^|[;&|]\s*|\s)git(?:\s+-C\s+\S+)?\s+commit\b")


def _untracked_as_diff(cwd: Path) -> str:
    """Render untracked, non-ignored files as an all-added diff."""
    parts: list[str] = []
    for rel in _git(["ls-files", "--others", "--exclude-standard"], cwd).splitlines():
        path = cwd / rel
        if path.suffix.lower() in _SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        parts.append(f"+++ b/{rel}\n@@ -0,0 +1,{len(lines)} @@")
        parts.extend("+" + line for line in lines)
    return "\n".join(parts)


def _commit_diff(command: str, cwd: Path) -> str:
    """Diff of what ``command`` (a git commit) may record.

    The hook runs before the whole command line, so ``git add x && git commit``
    has not staged anything yet when it is scanned. When the command stages
    files itself (or commits with -a), everything that could end up in the
    commit is scanned: tracked changes and untracked files.
    """
    before, after = command.split("commit", 1)
    commit_all = re.search(r"\s(?:-a\b|-\w*a\w*\b|--all\b)", after)
    stages_first = re.search(r"\bgit\s+(?:-C\s+\S+\s+)?add\b", before)
    if stages_first:
        return _git(["diff", "HEAD", "-U0", "--no-color"], cwd) + "\n" + _untracked_as_diff(cwd)
    return _git(["diff", "HEAD" if commit_all else "--cached", "-U0", "--no-color"], cwd)


def _report(findings: list[Finding]) -> str:
    lines = [str(finding) for finding in findings[:30]]
    if len(findings) > 30:
        lines.append(f"... and {len(findings) - 30} more")
    return "\n".join(lines)


def hook_main() -> int:
    event = read_input()
    command = str((event.get("tool_input") or {}).get("command") or "")
    if not _COMMIT_RE.search(command):
        return ALLOW
    cwd = Path(event.get("cwd") or ".")
    terms = load_forbidden_terms(project_dir(event)) or load_forbidden_terms(cwd)
    findings = scan_diff(_commit_diff(command, cwd), terms)
    if findings:
        print(
            "Blocked by .claude/hooks/secret_scan.py: the commit would add secrets or "
            "forbidden terms. Remove them (do not just reword the commit):\n" + _report(findings),
            file=sys.stderr,
        )
        return BLOCK
    return ALLOW


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Scan for secrets and forbidden terms.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="scan the staged diff")
    mode.add_argument("--all", action="store_true", help="scan every tracked file")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    terms = load_forbidden_terms(root)
    if args.staged:
        findings = scan_diff(_git(["diff", "--cached", "-U0", "--no-color"], root), terms)
    else:
        findings = scan_tracked_files(root, terms)

    if findings:
        print("secret_scan: problems found:\n" + _report(findings), file=sys.stderr)
        return 1
    scope = "staged diff" if args.staged else "tracked files"
    print(f"secret_scan: {scope} clean ({len(terms)} forbidden term(s) checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]) if len(sys.argv) > 1 else hook_main())
