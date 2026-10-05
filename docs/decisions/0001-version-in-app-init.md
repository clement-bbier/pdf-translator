# 0001. The app version lives in `app/__init__.py`

- Status: Accepted
- Date: 2026-10-04

## Context

The four module CLIs (`app.pipeline`, `app.extractor`, `app.rebuilder`,
`app.scanned`) need a `--version` flag, and `pyproject.toml` needs the same
number. The package is never installed: the Makefile and CI install
`requirements*.txt` only and run from the source tree. Therefore
`importlib.metadata.version("pdf-translator")` would raise
`PackageNotFoundError` in the venv and in CI.

## Decision

- `__version__` in `app/__init__.py` is the single source of truth. The CLIs
  print `pdf-translator <version>` from it.
- `pyproject.toml` declares `dynamic = ["version"]` and reads it with
  `[tool.setuptools.dynamic] version = {attr = "app.__version__"}`.
- A `[build-system]` table (`setuptools>=61`, `setuptools.build_meta`) was
  added because the `attr` lookup needs a build backend that supports it.
  Package discovery is restricted to `app*` so that `tests/`, `evaluation/`
  and `.claude/` are not picked up.
- A release bumps `app/__init__.py` only.

## Alternatives considered

- `importlib.metadata`: requires an installed package, which this project
  does not have.
- Version duplicated in `pyproject.toml` and the code: the two values drift.
- Reading `pyproject.toml` at runtime: adds file parsing and breaks when the
  file is not shipped next to the code.

## Consequences

- One edit per release; no install step is needed to read the version.
- `[build-system]` is build-time only. Nothing in the Makefile or CI builds
  the package, so setuptools is not a runtime or test dependency.
- Importing `app` for the version must stay free of side effects, since
  setuptools evaluates the attribute at build time.

## Known follow-up (security audit)

The floor `setuptools>=61` admits versions affected by CVE-2022-40897,
CVE-2024-6345 and CVE-2025-47273. It should be raised to `>=78.1.1`. Risk is
low today: nothing builds the package, and pip's isolated build fetches the
latest setuptools anyway.
