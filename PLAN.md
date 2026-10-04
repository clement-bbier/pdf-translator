# PLAN

## Part A - Agentic development system

### A.0 Formats verified against the official Claude Code documentation

Checked on 2026-10-04 against Claude Code 2.1.288 and code.claude.com/docs
(sub-agents, hooks, skills, settings/permissions, worktrees, headless).

| Topic | Verified fact | Consequence here |
| --- | --- | --- |
| Subagents | `.claude/agents/*.md`, YAML frontmatter: `name`, `description` (drives delegation), `tools` (comma list), `model` (`opus`/`sonnet`/`haiku`/full id/`inherit`), `isolation: worktree`, `skills` (preloaded), `hooks` (agent-scoped), `color`, `maxTurns` | One file per agent, explicit tool lists, implementer uses `isolation: worktree` |
| Subagent hooks | Settings hooks fire inside subagents; tool events carry `agent_id`/`agent_type`. Frontmatter `hooks` of project agents run only after the workspace trust dialog is accepted, never in `claude -p` | Role guard wired in settings.json, role read from `agent_type` |
| Worktree isolation | Worktree under `.claude/worktrees/<name>/` on branch `worktree-<name>`; base is the remote default branch unless `worktree.baseRef: "head"`; edits/commands targeting the main checkout are blocked; kept on disk when it has commits | `baseRef: head`; implementers write their handoff inside their worktree |
| Hook events | `SessionStart` (matchers startup/resume/clear/compact), `PreToolUse`, `PostToolUse`, `SubagentStart`, `SubagentStop` (input: `agent_id`, `agent_type`, `agent_transcript_path`, `stop_hook_active`, `last_assistant_message`, `cwd`) | Five events wired |
| Exit codes | 0 proceed (stdout JSON parsed); 2 = PreToolUse blocks the call, PostToolUse shows stderr to Claude, SubagentStop prevents the subagent from stopping; other = non-blocking error | Guards and validators exit 2 with the reason on stderr |
| Hook config | `hooks.<Event>[] = {matcher, hooks: [{type: "command", command, timeout}]}`; `$CLAUDE_PROJECT_DIR` stays at the project root inside worktrees, `cwd` follows the agent; settings changes picked up by a file watcher | Commands use `$CLAUDE_PROJECT_DIR`; hooks read `cwd` |
| Skills | `.claude/skills/<name>/SKILL.md`, frontmatter `description`, `argument-hint`, `disable-model-invocation`; `$ARGUMENTS`; `/name` works in `-p` mode; `.claude/commands/` still works but skills are recommended | /feature, /eval, /release-check are skills with `disable-model-invocation: true` |
| Permissions | `permissions.allow/deny`, `Tool(specifier)`, `Bash(cmd *)` prefix wildcard, `Read(**/.env)` gitignore-style paths; deny cannot carve exceptions | Deny `.env` reads + destructive commands; `.env.*` except `.env.example` handled by a hook |
| Loading | New `.claude/agents` directory needs a session restart; headless `claude -p` loads project agents, skills and hooks | The demo runs in a fresh headless session |

### A.1 Structure created

- [x] `CLAUDE.md`: overview, architecture map, commands, conventions, definition of done, critical rules, agents
- [x] `.claude/settings.json`: minimal allow list, deny `.env` reads and destructive commands, `worktree.baseRef: head`, hook wiring
- [x] `.claude/agents/`: planner, implementer, reviewer, evaluator, security-auditor, docs-writer
- [x] `.claude/skills/`: eval-protocol, decision-record, security-checklist (procedures); feature, eval, release-check (entry points)
- [x] `.claude/hooks/`: guard_bash, guard_write, guard_read, guard_role, secret_scan, lint_python, record_subagent_start, validate_handoff, session_context, run_hook (launcher), handoff (schema model)
- [x] `.claude/schemas/handoff.schema.json` exported from the Pydantic model
- [x] `evaluation/`: layout suite + committed baseline + comparison (gives /eval and the evaluator real work before the translation suite exists)
- [x] `scripts/validate_agentic_config.py`, `scripts/run_tests.py`, `scripts/install_git_hooks.py`, `Makefile`
- [x] `tests/test_hooks.py` (24 tests, every hook), `tests/test_agentic_config.py` (validator mutation tests)
- [x] CI: lint, validate, secret scan, tests, eval
- [x] `docs/agentic-development.md`
- [x] Demonstration: `/feature` adds `--version` to the CLI, end to end, in a headless session (merge `4f72661`; lessons in docs/agentic-development.md)

## Part B - Production upgrade: RAG, verification, evaluation, containers

Every workstream below runs through `/feature` (planner, human-approved plan,
implementers in worktrees, review, evaluation, audit, docs, merge), so each
leaves its plan, review and evaluation trace in the merge commit.

### B.0 Current architecture

```text
FastAPI (main.py) -> pipeline.run_job -> extractor (PyMuPDF blocks)
                                      -> TranslationCache (hash src|tgt|text, tmp/cache.json)
                                      -> Translator protocol
                                           MockTranslator   ("[tgt] text" grown 1.4x, tests)
                                           GatewayTranslator (OpenAI-compatible HTTP, OAuth2
                                                              device flow, retry, JSON parsing,
                                                              prompts hard-coded in translator.py)
                                      -> rebuilder (redact + reinsert, shrink/truncate)
                                      -> scanned (multimodal page path, optional)
```

Gaps against the target: prompts are string constants without a version;
model and temperature are not pinned (no temperature sent at all); no record
of what was sent and returned; the cache key ignores model and prompt (a model
switch serves stale translations); no terminology control; output is trusted
once it parses as JSON; no translation-quality measurement; no container, CI
only from Part A; README describes the project as an internal tool.

### B.1 Target architecture

```text
pipeline -> LLMTranslator (implements Translator; MockTranslator kept for layout tests)
              -> PromptBuilder: prompts/v1/*.md + context providers
                   GlossaryProvider (exact match, CSV)          [A]
                   TranslationMemoryProvider (embeddings, top-3) [A]
              -> CachedProvider (key: model, prompt version, params, request)
              -> TracingProvider (JSONL line per call)
              -> LLMProvider protocol
                   AnthropicProvider (real adapter used here: Claude Messages API,
                                      claude-haiku-4-5-20251001 at temperature 0)
                   GatewayProvider   (OpenAI-compatible gateway: existing HTTP/OAuth/retry)
                   FakeProvider      (deterministic, tests and CI)
              -> Verifier: JSON, segment count, glossary, numbers, dates  [B]
                   policy: one retry with the errors, then flag for human review
evaluation/ -> layout suite (Part A) + translation suite: 30 zh->en public
               segments, 3 configs x 3 runs, report in docs/            [C]
Docker multi-stage + compose + password-protected UI + CI build/eval    [D]
```

Phase 1 defines the extension points (context providers, verifier hook,
`rag.*` / `verification.*` config keys as no-ops) so A, B, C and D can be
built in parallel against stable interfaces and merged independently.

### B.2 Tasks

Phase 1 - foundation (one /feature, tasks in dependency order)

- [ ] F1 `LLMProvider` protocol + `AnthropicProvider` (Claude) + `GatewayProvider` (moved from GatewayTranslator) + deterministic `FakeProvider`
- [ ] F2 Pinned parameters in config.yaml: dated model id (claude-haiku-4-5-20251001), temperature 0, prompt version; claude-sonnet-5-5 as comparison model (it rejects sampling parameters)
- [ ] F3 Versioned prompts in `prompts/v1/`, prompt version recorded with each translation
- [ ] F4 JSONL trace per call: input id, prompt version, model, params, output, duration, estimated cost
- [ ] F5 Cache keyed by input + prompt version + model (+ params)

Phase 2 - four parallel workstreams (one /feature each, one worktree session each)

- [ ] A RAG: finance glossary CSV (~50 terms, exact match, injected); translation memory with embeddings, top-3 examples, config switch
- [ ] B Verification: code checks on every output; retry with errors, then human-review flag; never silent delivery
- [ ] C Evaluation: 30 public zh->en segments with cited sources; glossary adherence, check pass rate, similarity, cost, latency; `make eval` report comparing 3 configs, 3 runs each
- [ ] D Container and CI: multi-stage Dockerfile, non-root, CJK fonts, lock file; compose; password-protected UI; CI lint/test/build/eval

Phase 3 - integration and proof

- [ ] Merge A-D (tests green at each merge), full suite, image build, app in the container, end-to-end translation of a public demo document
- [ ] `make eval` on the real model if a key is configured, otherwise on the fake model, labelled as such
- [ ] README (what, mermaid architecture, 3-command start, evaluation results), docs/decisions/, docs/how-it-works.md
- [ ] Final checks: no secret, no employer reference (search), CI green

### B.3 Dependencies

```text
F1 -> F2 -> F3 -> F4, F5 (F4 and F5 parallel)
F* -> A, B, C, D (parallel)
C reads A's and B's config switches; its final numbers are produced after A and B merge (Phase 3)
A, B, C, D -> Phase 3
```

### B.4 Risks

| Risk | Mitigation |
| --- | --- |
| Refactoring the translator breaks current behaviour | Keep the `Translator` protocol and `MockTranslator`; existing 94 tests stay green at every merge |
| Real-model cost and availability | Personal Anthropic key in `.env` (never committed); CI and tests use the fake model; the real evaluation is a manual workflow using a repository secret |
| Current Claude models reject `temperature` and have no dated id | Primary model Claude Haiku 4.5 (dated, temperature 0); Sonnet 5.5 compared without temperature; decision note |
| Fake model makes RAG look good by construction | Report labels fake-model numbers as plumbing checks, not quality claims |
| Public bilingual data: licensing, accuracy | Short extracts, each with its source URL; text copied verbatim from the published documents |
| Traces contain document text | Local, gitignored, excluded from the Docker context; documented in a decision note |
| Embedding model would add a heavy dependency (torch) | Local character n-gram hashing embedder by default, gateway embeddings optional; decision note |
| Parallel workstreams conflict at merge (config.yaml, README, Makefile) | Phase 1 adds all config keys and Makefile targets up front; conflicts resolved at integration with tests |
| Temperature 0 is not bit-for-bit deterministic | Cache + 3-run stability measurement; decision note |
