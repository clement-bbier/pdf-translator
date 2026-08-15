# PDF Translator

Internal tool for translating PDF contracts (CJK → FR/EN) through a single-page
web UI. AI is used **only** for translation; extraction, layout, and
reconstruction are fully deterministic.

## Purpose

Upload up to a handful of PDF contracts, pick a source and target language,
and get back translated PDFs that preserve the original layout as closely as
possible — text position, font size (shrunk if needed), and color. No content
is stored beyond the lifetime of a job.

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
  (`translate_blocks(blocks, src, tgt) -> list[str]`) plus two
  implementations: `MockTranslator` (deterministic, no network, used in
  tests and local development) and `GatewayTranslator` (calls an
  OpenAI-compatible chat/completions endpoint through the internal LLM
  gateway). The rest of the codebase only ever talks to this interface —
  nothing else knows which provider or model is behind it.
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
  (scoped by source/target language pair), persisted to a local JSON file
  for the session.
- **auth.py** — OAuth2 device-code flow used to authenticate
  `GatewayTranslator` against the internal LLM gateway. See
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
```

## Authentication

`GatewayTranslator` (used when `provider: gateway` in `config.yaml`)
authenticates against the internal LLM gateway with the standard OAuth2
**device authorization grant** (RFC 8628), implemented in `auth.py`:

1. **Device authorization** — POST to a device authorization endpoint;
   receive a `device_code`, a `user_code`, a `verification_uri`, and an
   `expires_in`.
2. **User approval** — the `verification_uri` is displayed (or opened) so the
   user can approve the request in their browser, using their existing
   corporate SSO session. This application never handles the user's
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

Ask the team that manages the internal LLM gateway for the five values listed
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
   your normal corporate SSO login. This app never sees your password.
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
consumes their own internal quota; there is no shared service account and no
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
provider: mock          # mock | gateway
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
  pydantic, python-dotenv, httpx, pyyaml) without discussion.
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

- **No content persistence beyond a job's lifetime.** Uploaded and generated
  files live under per-job directories and are deleted when the result is
  downloaded — and equally when a job fails mid-run (no partial output is
  left behind).
- **Automatic purge.** Any job (downloaded or not) older than 30 minutes is
  purged automatically.
- **Tokens kept in memory only.** OAuth2 access and refresh tokens obtained
  by `auth.py` are never written to disk.
- **No content in logs.** Server logs only ever carry job IDs and counters
  (file counts, status, duration) — never file names or document content.

## License

[MIT](LICENSE).
