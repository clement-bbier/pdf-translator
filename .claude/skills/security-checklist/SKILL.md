---
name: security-checklist
description: Security and confidentiality checklist for this repository - secrets, sensitive data, employer/client references, logging, uploads, dependencies, container and CI. Use when auditing a diff or a release (security-auditor agent, /feature, /release-check).
---

# Security checklist

Run the scanners first, then read the diff (or the tracked files for a
release) with each item below in mind. Cite `file:line` for every finding.

## 1. Scanners

```
make security                     # secrets + forbidden terms in tracked files
git diff <base>...HEAD --stat     # scope of the change (skip for a release)
make audit                        # pip-audit on requirements.txt (needs network)
```

`make security` reads the local, untracked `.claude/forbidden-terms.local.txt`
(employer, client and internal host names). If that file is absent, say so in
the report: only generic secret patterns were checked. If `make audit` cannot
reach the network, report it as not run rather than passed.

## 2. Secrets and sensitive data

- No key, token, password or connection string in code, config, tests,
  fixtures, docs, notebooks or CI files. Secrets come from `.env` (gitignored)
  or CI secrets, and `.env.example` holds names only.
- `.gitignore` covers `.env`, `.env.*` (except `.env.example`), local term
  lists, handoffs, logs, traces and caches.
- Test and evaluation data are synthetic or public, each public extract with
  its source cited. No real contract, no personal data.
- No employer, client or internal URL anywhere, including comments, commit
  messages and docs.

## 3. Runtime data handling

- Logs carry ids and counters only: never document text, file names, tokens
  or prompts. LLM traces (if any) stay local and gitignored.
- HTTP error responses never echo exceptions, paths or user file names.
- Uploads: size and type validated before processing; job files purged.
- Caches and translation memories are local files, excluded from git and
  from the Docker build context.

## 4. Dependencies

- Every dependency pinned (`requirements.txt`, lock file); each new one
  justified in a decision note or the commit.
- No known vulnerability (`make audit`); if one exists, say whether the
  vulnerable path is reachable.

## 5. Container and CI (when present)

- Image runs as a non-root user; no secret baked into layers or build args;
  `.dockerignore` excludes `.env`, `.git`, caches, outputs.
- Exposed web UI requires the password from an environment variable.
- CI never prints secrets, uses the fake model for tests and evaluation, and
  pins third-party actions to a version.
