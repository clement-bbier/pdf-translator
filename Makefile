# Developer entry points. Every target works from the main checkout and from a
# git worktree (worktrees have no venv: the main checkout's one is used).

# Main checkout = parent of the shared .git directory.
MAIN_ROOT := $(patsubst %/.git,%,$(shell git rev-parse --path-format=absolute --git-common-dir 2>/dev/null))
VENV_PY := $(firstword $(wildcard .venv/Scripts/python.exe .venv/bin/python \
	$(MAIN_ROOT)/.venv/Scripts/python.exe $(MAIN_ROOT)/.venv/bin/python))
PYTHON ?= $(if $(VENV_PY),$(VENV_PY),python)

.PHONY: help install test lint format check eval eval-baseline validate security audit \
	hooks-install lock docker-build docker-up eval-real eval-report

help:
	@echo "install        install runtime + dev dependencies"
	@echo "test           run every test module (incl. hook tests)"
	@echo "lint           ruff lint + format check"
	@echo "format         ruff format + autofix"
	@echo "eval           run the evaluation and compare with the baseline"
	@echo "eval-baseline  HUMAN ONLY: accept the current evaluation numbers"
	@echo "eval-real      translation suite with the real model (needs ANTHROPIC_API_KEY)"
	@echo "eval-report    publish evaluation/reports/translation.md to docs/evaluation-report.md"
	@echo "validate       check the agentic configuration (.claude/)"
	@echo "security       secret/forbidden-term scan of tracked files"
	@echo "audit          dependency vulnerability audit (network)"
	@echo "lock           regenerate requirements.lock with hashes (uv, network)"
	@echo "docker-build   build the container image pdf-translator:local"
	@echo "docker-up      build and start the container with docker compose"
	@echo "check          lint + test + validate + security (pre-push gate)"

install:
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	$(PYTHON) scripts/run_tests.py

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

eval:
	$(PYTHON) -m evaluation.run

eval-baseline:
	$(PYTHON) -m evaluation.run --update-baseline

eval-real:
	$(PYTHON) -m evaluation.run --provider anthropic

eval-report:
	$(PYTHON) -m evaluation.run --publish-report

validate:
	$(PYTHON) scripts/validate_agentic_config.py

security:
	$(PYTHON) .claude/hooks/secret_scan.py --all

audit:
	$(PYTHON) -m pip_audit -r requirements.txt

hooks-install:
	$(PYTHON) scripts/install_git_hooks.py

check: lint test validate security

lock:
	$(PYTHON) -m uv pip compile requirements.txt --generate-hashes --python-version 3.12 --python-platform x86_64-unknown-linux-gnu -o requirements.lock

docker-build:
	docker build --load -t pdf-translator:local .

docker-up:
	docker compose up --build
