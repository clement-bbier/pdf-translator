# PDF Translator

Tool for translating PDF contracts (CJK → FR/EN) through a single-page
web UI. AI is used **only** for translation; extraction, layout, and
reconstruction are fully deterministic.

## Purpose

Upload up to a handful of PDF contracts, pick a source and target language,
and get back translated PDFs that preserve the original layout as closely as
possible — text position, font size (shrunk if needed), and color. Uploaded
and generated PDFs are deleted with the job. Translated text, however, is
also written to local cache and trace files that outlive the job; see
[Data that persists after a job](#data-that-persists-after-a-job).

## Architecture

```text
                 ┌─────────────┐
   browser  ───► │   main.py   │  FastAPI: single-page UI, job endpoints
                 └──────┬──────┘
                        │ BackgroundTasks
                        ▼
                 ┌─────────────┐
                 │ pipeline.py │  orchestrates one job end to end
                 └──────┬──────┘
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
 ┌─────────────┐ ┌─────────────┐ ┌──────────────┐
 │extractor.py │ │translator.py│ │ rebuilder.py │
 │  (PyMuPDF)  │ │             │ │  (PyMuPDF)   │
 │ text blocks │ │ Translator  │ │   redact +   │
 │ + layout    │ │ interface   │ │  reinsert    │
 └─────────────┘ └──────┬──────┘ └──────────────┘
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
        ┌───────────┐     ┌──────────────┐
        │ cache.py  │     │   auth.py    │
        │ dedup by  │     │ device-code  │
        │ block hash│     │ OAuth2 flow  │
        └───────────┘     └──────────────┘
```

- **main.py** — FastAPI app: serves the single HTML page, accepts uploads,
  runs jobs in the background, exposes status polling and a zip download.
- **pipeline.py** — orchestrates one job: extract → deduplicate → translate →
  rebuild, per file.
- **extractor.py** — uses PyMuPDF to pull text blocks with their coordinates,
  font, and color out of a PDF, and flags scanned (non-extractable) pages.
- **translator.py** — defines the `Translator` interface
  (`translate_blocks(blocks, src, tgt) -> list[str]`), `MockTranslator`
  (deterministic, no network, the default), `GatewayTranslator` (a
  compatibility alias of `LLMTranslator` over the gateway provider) and
  `get_translator(config)`, which builds the translator named by `provider`.
  The rest of the codebase only talks to the `Translator` interface.
- **llm/** — the LLM layer behind `LLMTranslator`: the `LLMProvider`
  protocol (`base.py`), providers for Anthropic, an OpenAI-compatible gateway
  and a deterministic fake, `PromptBuilder` (reads `prompts/<version>/`), the
  shared retry policy, `factory.py` (base provider, then `CachedProvider`,
  then `TracingProvider`). See
  `docs/decisions/0002-llm-provider-interface-and-fake-model.md`.
- **config.py** — loads `config.yaml` and validates the `llm`, `rag`,
  `verification`, `tracing` and `cache` sections.
- **rebuilder.py** — redacts the original text blocks and reinserts the
  translated text in the same area, shrinking the font size or falling back
  to truncation when the translation doesn't fit.
- **scanned.py** — optional multimodal path for scanned pages (no extractable
  text): renders the page to PNG for the model and rebuilds it as a clean
  translated text page. Gated behind `multimodal_enabled` (off by default).
- **fonts.py** — picks a font per script (Latin/Chinese/Japanese/Korean) and
  detects characters a font would silently drop. Uses only fonts embedded in
  the `pymupdf` package — nothing to install on the machine.
- **cache.py** — deduplicates translation calls by hashing each text block
  (scoped by model, prompt version and source/target language pair),
  persisted to `tmp/cache.json`. The file is not purged after a job; see
  [Data that persists after a job](#data-that-persists-after-a-job).
- **auth.py** — OAuth2 device-code flow used to authenticate
  `GatewayTranslator` against an OpenAI-compatible LLM gateway protected by an OAuth2 device-code flow. See
  [Authentication](#authentication) below.

## Quick start

### Try it without a gateway

The default configuration (`provider: mock` in `config.yaml`) works
immediately after install: **no gateway, no account, no `.env` file, no
configuration of any kind**. The mock provider produces deterministic
placeholder translations locally, so you can clone the repo and see the full
upload → translate → download cycle — layout preservation included — before
ever thinking about connecting a real translation backend.

### Install and run

Requires Python 3.11+ (developed and tested with 3.12).

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If only one Python version is installed, `python -m venv .venv` works too.

No font installation is required on the machine: CJK rendering uses fonts
embedded inside the `pymupdf` package itself (audited — the OS font directory
is never consulted), so the output is identical on a bare Windows, Linux, or
macOS machine.

Copy the environment template and fill it in if you plan to use the real
gateway provider (skip this for local development with the mock provider):

```powershell
copy .env.example .env
```

Run the app:

```powershell
uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000/` in a browser.

Every module is also independently testable from the CLI, e.g.:

```powershell
python -m app.extractor tests/fixtures/simple.pdf
python -m app.pipeline tests/fixtures/simple.pdf --src zh --tgt fr --provider mock
python -m app.pipeline --version
```

Every module CLI accepts `--version` and prints `pdf-translator <version>`.

## Authentication

`GatewayTranslator` (used when `provider: gateway` in `config.yaml`)
authenticates against an OpenAI-compatible LLM gateway with the standard OAuth2
**device authorization grant** (RFC 8628), implemented in `auth.py`:

1. **Device authorization** — POST to a device authorization endpoint;
   receive a `device_code`, a `user_code`, a `verification_uri`, and an
   `expires_in`.
2. **User approval** — the `verification_uri` is displayed (or opened) so the
   user can approve the request in their browser, using their existing
   identity provider's SSO session. This application never handles the user's
   credentials directly.
3. **Polling** — the token endpoint is polled with the `device_code` at the
   interval the gateway specifies, until the request is approved, denied, or
   expires.
4. **In-memory storage** — the resulting `access_token` and `refresh_token`
   are kept in memory only, for the lifetime of the process. They are never
   written to disk.
5. **Proactive refresh** — the access token is refreshed shortly before it
   expires, transparently to callers.

`get_token()` is the only public function of `auth.py`; it is the sole entry
point `GatewayTranslator` uses to obtain a token.

**Configuration** — the endpoint URLs, `client_id`, and scopes come
exclusively from environment variables, loaded from a local `.env` file
(gitignored, never committed). See `.env.example` for the annotated template
and [Connecting to the real gateway](#connecting-to-the-real-gateway) for the
step-by-step setup. No secret is ever hardcoded or stored in `config.yaml`.

## Connecting to the real gateway

By default the app runs with `provider: mock` and needs no credentials at all.
Follow these steps only when you want real translations.

### 1. Get the values

Ask the gateway administrator for the five values listed
in `.env.example`. They are all non-secret configuration except the
`client_id`, which identifies this application to the identity provider:

| Variable | What it is |
| --- | --- |
| `GATEWAY_BASE_URL` | Base URL of the OpenAI-compatible API (`/chat/completions` is appended) |
| `GATEWAY_DEVICE_ENDPOINT` | OAuth2 device authorization endpoint (RFC 8628) |
| `GATEWAY_TOKEN_ENDPOINT` | OAuth2 token endpoint, used for sign-in and refresh |
| `GATEWAY_CLIENT_ID` | OAuth2 client_id registered for this application |
| `GATEWAY_SCOPES` | Space-separated scopes the issued token must carry |

### 2. Fill in `.env`

```powershell
copy .env.example .env
```

Then edit `.env` and paste each value after its `=`. `.env` is gitignored —
never commit it, and never move these values into `config.yaml`.

### 3. Switch the provider

In `config.yaml`, change:

```yaml
provider: gateway       # was: mock
```

Optionally pick a different default `model` from the `models` list in the same
file — the UI select lets you change it per job.

### 4. Sign in on first launch

Start the app (`uvicorn app.main:app --reload`) and open it in a browser. With
`provider: gateway` the page shows a **Sign in** button and the **Translate**
button stays disabled until a session exists.

1. Click **Sign in**. The page displays a short user code and a link.
2. Open that link (it opens in a new tab), enter the code, and approve with
   your normal SSO login. This app never sees your password.
3. Leave the original tab open — it polls until approval lands, then switches
   to "Connected to the gateway" and enables **Translate**.

The token lives in the server process's memory only; it is never written to
disk. Restarting the app means signing in again.

### If the token expires mid-job

It is refreshed automatically. `get_token()` renews the access token once it
comes within 60 seconds of expiry, using the refresh token obtained at
sign-in, so a long job spanning the token's lifetime keeps working without
user interaction.

If the refresh itself fails (for example the refresh token was revoked), the
affected batch fails like any other transient error: it is retried, then left
in the source language and reported in `JobResult.warnings`. The job as a
whole still completes and the PDF is still produced — a failed batch never
kills the job. Sign in again to restore normal translation.

## Deployment model

**Today — a local POC.** One process serves one user. You run the app on your
own machine, sign in with your own SSO account, and the resulting token is
held in memory in that process only, for its lifetime. Each user therefore
uses their own quota; there is no shared service account and no
credential is shared between colleagues.

**Model choice is left to the user.** `config.yaml` lists the available models
and the UI select lets you switch per job, so the cost/quality trade-off is
yours to make: lighter models for bulk or draft work, larger ones for dense
legal wording that must not drift.

**Known limitation if this is ever deployed as a shared service.** The token
is stored in a module-level variable (`app/auth.py`), which is correct for one
process serving one user but *not* for several users pointed at one server
instance: they would all share whichever token was stored last. Moving to that
model requires per-user-session storage — the token tied to an authenticated
HTTP session (a session cookie), not a module global — plus a per-session
sign-in flow. This is **not implemented today** and should be planned before
any multi-user deployment. The same note is recorded in `app/auth.py`, above
the in-memory state it applies to.

## Configuration (`config.yaml`)

```yaml
provider: mock          # mock | fake | anthropic | gateway
model: gemini-2.5-flash # default model (provider: gateway only)
models:                 # choices offered in the UI select
  - gemini-2.5-flash-lite
  - gemini-2.5-flash
  # ... see config.yaml for the full list
batch_size: 20          # text blocks per translation API call
multimodal_enabled: false  # translate scanned pages by sending a page image
max_files: 10           # max PDFs per job
max_file_mb: 20          # max size per uploaded PDF
font_min_scale: 0.6     # smallest allowed font shrink ratio before truncating
languages:
  source: [zh, ja, ko, en, fr]
  target: [fr, en]
```

`config.yaml` never contains secrets — only non-sensitive settings. Switch
`provider` to `gateway` to use the real translation backend (requires `.env`
to be configured as described above).

### Providers

`provider` selects the backend. The default is `mock`.

| Value | Backend | Needs |
| --- | --- | --- |
| `mock` | `MockTranslator`: deterministic placeholders, no LLM code path | nothing |
| `fake` | `FakeProvider` behind `LLMTranslator`: deterministic, offline, exercises prompts, cache and tracing | nothing |
| `anthropic` | Anthropic Messages API | `ANTHROPIC_API_KEY` in `.env` |
| `gateway` | OpenAI-compatible gateway (OAuth2 device flow) | the `GATEWAY_*` variables, see above |

`ANTHROPIC_API_KEY` is read from `.env` (the name is listed in
`.env.example`; the value is never in `config.yaml` and never logged). The
cache and tracing wrappers below apply to `fake`, `anthropic` and `gateway`,
not to `mock`.

### `llm:` section

Pinned parameters for `fake`, `anthropic` and `gateway`. The `gateway`
provider takes its model from the top-level `model` key instead of
`llm.model`.

```yaml
llm:
  model: claude-haiku-4-5-20251001   # dated model id, not an alias
  temperature: 0                     # null = not sent
  max_tokens: 4096
  timeout: 60                        # seconds
  prompt_version: v1                 # reads prompts/<version>/
  compare_models:                    # evaluation only
    - {model: claude-sonnet-5-5, temperature: null}
  prices:                            # USD per million tokens, for cost estimates
    claude-haiku-4-5-20251001: {input: 1.00, output: 5.00}
    claude-sonnet-5-5: {input: 2.00, output: 10.00}
```

- Unknown keys and invalid values fail validation at load time.
- Temperature 0 does not guarantee identical outputs; see
  `docs/decisions/0003-temperature-zero-not-deterministic.md`.
- Sonnet/Opus 5.x models reject sampling parameters, so `temperature` is
  omitted for ids starting with `claude-sonnet-5` or `claude-opus-5`.
- `compare_models` lists models to compare against the main one in
  evaluation. It has no effect on a normal translation job.
- `prices` feed the `estimated_cost_usd` field of the trace. A model without
  an entry gets no cost estimate.
- Prompts are versioned files under `prompts/<prompt_version>/`. Changing
  `prompt_version` also changes cache keys.

### `rag` and `verification`

`rag` is described in [Terminology and translation memory](#terminology-and-translation-memory).
`verification` is described in [Verification and review](#verification-and-review).

### `tracing` and `cache`

```yaml
tracing: {enabled: true, path: traces/llm-calls.jsonl}
cache:   {enabled: true, path: tmp/llm-cache.json}
```

- `tracing` appends one JSON line per LLM call: ids, model, token counts,
  duration, estimated cost, cache hit, status, and the **model output
  (translated text)**. Input messages are not stored.
- `cache` stores LLM responses keyed by a hash of the whole request, so an
  identical request is answered from disk without calling the model. Failed
  calls and responses the translator rejects are not kept.

Both are on by default and both write translated text to disk; see below.

### Data that persists after a job

| File | Content | Written when |
| --- | --- | --- |
| `traces/llm-calls.jsonl` | model output (translated text) plus metadata | `tracing.enabled`, providers other than `mock` |
| `tmp/llm-cache.json` | LLM responses (translated text) | `cache.enabled`, providers other than `mock` |
| `tmp/cache.json` | per-block translations, keyed by hash | every provider, including `mock` |

None of these is removed by the per-job cleanup or the 30-minute purge, and
none has a size cap or expiry. They stay on the local machine: `traces/` and
`tmp/*` are gitignored, and the application logger never receives their
content (see `docs/decisions/0004-traces-are-local-only.md`). Do not copy
them into an image, archive or release.

To stop writing them, set `tracing.enabled: false` and `cache.enabled: false`
in `config.yaml`. `tmp/cache.json` has no switch. To remove what exists,
delete the files (stop the app first):

```powershell
Remove-Item traces\llm-calls.jsonl, tmp\llm-cache.json, tmp\cache.json -ErrorAction SilentlyContinue
```

## Terminology and translation memory

Two optional retrieval sources add context to the translation prompt
(`app/rag/`). Both are configured in the `rag:` block of `config.yaml`.

- **Glossary** — 59 finance terms (`data/glossary/finance_zh.csv`, columns
  `source,target_fr,target_en,domain`). Terms found in a batch by exact,
  longest-match-first search are injected as required renderings. Applies to
  source language `zh` and targets `fr`/`en`. Rationale:
  [decision 0002](docs/decisions/0002-exact-glossary-search.md).
- **Translation memory** — validated Chinese/English pairs
  (`data/tm/seed_zh_en.jsonl`) similar to the text are injected as examples,
  ranked by cosine similarity of local hashing embeddings. Rationale:
  [decision 0003](docs/decisions/0003-hashing-embedder-for-translation-memory.md).

| Key | Default | Meaning |
| --- | --- | --- |
| `rag.glossary` | `true` | Enable the glossary |
| `rag.glossary_path` | `data/glossary/finance_zh.csv` | Glossary CSV |
| `rag.translation_memory` | `false` | Enable the translation memory |
| `rag.tm_path` | `data/tm/seed_zh_en.jsonl` | Translation memory records (JSONL) |
| `rag.tm_top_k` | `3` | Max examples injected per batch (1..20) |
| `rag.tm_min_score` | `0.3` | Minimum cosine similarity to keep an example (-1..1); a starting value |
| `rag.embedder` | `hashing` | `hashing` (local, stdlib) or `gateway` (embeddings endpoint) |
| `rag.embedding_model` | `text-embedding-3-small` | Model used when `embedder` is `gateway` |

Limitations:

- The glossary sources are Traditional Chinese (Hong Kong usage); Simplified
  Chinese text does not match.
- The hashing embedder matches shared characters only: no paraphrase, no
  cross-script matching. The seed memory is zh to en only.
- With `rag.embedder: gateway`, segment text is sent to the gateway
  `/embeddings` endpoint whatever translation provider is selected.
- Provenance of the data files: [`data/SOURCES.md`](data/SOURCES.md).

## Verification and review

LLM-backed translators (not `mock`) have each batch answer checked by
deterministic code in `app/verification/` (no second model; rationale in
[docs/decisions/0002](docs/decisions/0002-deterministic-verification-retry-then-flag.md)):

| Check | Fails when |
| --- | --- |
| `json_valid` | the answer is not a JSON object |
| `segment_count` | a segment is missing from the answer |
| `glossary_terms` | a required glossary term is absent (only when a context provider supplies terms) |
| `numbers` | a number or currency of the source is not found in the translation |
| `dates` | a date of the source is not found in the translation |

**Policy.** A failing batch is re-sent once with the findings as corrections
(`prompts/v1/retry_corrections.md`). Per segment the best attempt is kept. A
segment that still fails is *flagged*: it stays in the PDF as translated, is
listed in `review.json`, and is never cached. If no attempt is valid JSON the
batch fails as usual (job warning, source text kept).

```yaml
verification: {enabled: true, max_retries: 1}   # max_retries: 0 to 3
```

`enabled: false` turns checks off. Unknown keys or out-of-range values are
rejected at start.

**`review.json`** is written only when at least one segment is flagged, in
`output/<job_id>/` and in the downloaded zip. Fields: `job_id`,
`source_language`, `target_language`, `flagged_count`, and `segments`, each
with `segment_id`, `page`, `file`, `source`, `translation` (null if missing),
`failed_checks`, `messages`. It contains file names and document text, so it
follows the job's lifetime: purged with the job, never logged.

**Where it shows.** The web UI displays "N segment(s) need human review (see
review.json in the download)"; the CLI prints the same count and the path of
`review.json`; the job warnings carry one line per file.

**Trace.** When `tracing` is enabled in `config.yaml`, one content-free line
per batch is appended to `verification.jsonl` (next to the tracing file):
check names, flags, counters and `input_ids` only.

**Limits.** Numbers spelled out in words and Chinese-numeral amounts outside
dates are not checked, and cache entries written before verification existed
are served unchecked.

## Known limitations

- **Scanned pages** — a page with no extractable text (a genuine scan) cannot
  go through the standard block-by-block path. Two behaviours, controlled by
  `multimodal_enabled` in `config.yaml`:
  - **`false` (default)** — the page is copied as-is into the output and a
    warning cover page lists which pages were skipped.
  - **`true`** — the page is rendered to a PNG at 150 dpi, sent to the model as
    an image, and rebuilt as a clean page of translated text (`scanned.py`).
    This **discards the original layout**: the output is plain reflowed text,
    not a reproduction of the scan. It needs a multimodal model on the gateway
    and costs far more tokens per page than the text path. If a page fails, it
    falls back to the copy-as-is behaviour with an explicit warning; the job
    always completes.
- **Rewritten text color** — the color used for translated text is the
  dominant color detected across the original block (see `extractor.py`,
  Phase 1), not a per-character reproduction. A block that mixes multiple
  colors will render entirely in whichever color covered the most characters.
- **Single-user token storage** — the gateway access token lives in the
  memory of the one server process, which is correct for this single-user
  POC (each user runs their own instance and signs in with their own SSO
  account) but would **not** be safe as-is for a shared multi-user
  deployment: all users of a shared instance would end up using whichever
  token was stored last. A shared deployment requires per-user-session token
  storage first — see [Deployment model](#deployment-model).
- **Truncation on extreme growth** — layout fidelity targets ~95%, not
  pixel-perfect reproduction. Font size is reduced to fit a longer
  translation, down to `font_min_scale`; if it still doesn't fit, the text is
  truncated with a `...` suffix. This is most likely to happen with a short
  original title paired with a much longer translation.

## Development

Conventions that bind every change:

- Python 3.11+, type hints everywhere; code, comments and docstrings in
  English.
- No dependency beyond `requirements.txt` (fastapi, uvicorn, pymupdf,
  pydantic, python-dotenv, httpx, pyyaml, anthropic, httpx2) without
  discussion. Choices are recorded in `docs/decisions/`.
- Every module is independently runnable from the CLI
  (`python -m app.extractor file.pdf`, `python -m app.scanned file.pdf ...`).
- Server logs carry counters and job IDs only — never document content, file
  names, tokens, or secrets. Secrets live in `.env` (gitignored), never in
  `config.yaml`.
- Transient translation errors are retried (3x, backoff); a failed batch is
  reported in the job's warnings and never kills the job.

Tests live in `tests/`, each runnable directly (no pytest dependency):

```powershell
python tests/test_roundtrip.py
python tests/test_translation.py
python tests/test_pipeline.py
python tests/test_main.py
python tests/test_auth.py
python tests/test_scanned.py
```

The gateway and auth tests never touch the network: every HTTP exchange is
served by `httpx.MockTransport`.

Fixtures under `tests/fixtures/` are generated PDFs (`tests/make_fixtures.py`)
— never real contracts.

## Security

- **Uploaded and generated PDFs do not outlive a job.** They live under
  per-job directories and are deleted when the result is downloaded, and
  equally when a job fails mid-run (no partial output is left behind).
- **Automatic purge.** Any job (downloaded or not) older than 30 minutes is
  purged automatically. This purge covers the job directories only.
- **Translated text persists locally, outside the job directories.** See
  [Data that persists after a job](#data-that-persists-after-a-job) for the
  files, and for how to delete them or turn them off.
- **Tokens kept in memory only.** OAuth2 access and refresh tokens obtained
  by `auth.py` are never written to disk.
- **No content in logs.** Server logs only ever carry job IDs and counters
  (file counts, status, duration) — never file names or document content.

## License

[MIT](LICENSE).
