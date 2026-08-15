"""FastAPI: single-page UI plus upload/status/download endpoints for translation jobs."""

from __future__ import annotations

import io
import logging
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from app import auth
from app import config as app_config
from app.pipeline import OUTPUT_DIR, JobResult, _validate_file, run_job

logger = logging.getLogger(__name__)

TMP_DIR = Path(__file__).resolve().parent.parent / "tmp"

JOB_TTL_SECONDS = 30 * 60

app = FastAPI(title="PDF Translator")


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: no unexpected exception may leak into a response.

    The client only ever sees a generic message — exception text can carry
    anything (a token in an auth error, a client_id, a filesystem path), so
    none of it is echoed. The full traceback goes to the server log, which by
    project rule never receives document content or secrets in the first
    place; ``request.url.path`` is one of our own route templates, never user
    data.
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


# --------------------------------------------------------------------------- #
# In-memory job registry
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class JobState:
    """In-memory state for one job, updated as it progresses."""

    job_id: str
    status: str = "pending"  # pending | running | done | error
    message: str = ""
    created_at: float = field(default_factory=time.monotonic)
    result: JobResult | None = None
    error: str | None = None
    downloaded: bool = False


_jobs: dict[str, JobState] = {}
_jobs_lock = threading.Lock()


def _purge_expired_jobs() -> None:
    """Delete tmp/output dirs and drop registry entries for jobs older than the TTL."""
    now = time.monotonic()
    with _jobs_lock:
        expired = [
            job_id
            for job_id, state in _jobs.items()
            if now - state.created_at > JOB_TTL_SECONDS
        ]
        for job_id in expired:
            del _jobs[job_id]

    for job_id in expired:
        shutil.rmtree(TMP_DIR / job_id, ignore_errors=True)
        shutil.rmtree(OUTPUT_DIR / job_id, ignore_errors=True)
        logger.info("job %s purged (ttl expired)", job_id)


def _cleanup_job_dirs(job_id: str) -> None:
    """Remove a job's tmp and output directories."""
    shutil.rmtree(TMP_DIR / job_id, ignore_errors=True)
    shutil.rmtree(OUTPUT_DIR / job_id, ignore_errors=True)


# --------------------------------------------------------------------------- #
# HTML page
# --------------------------------------------------------------------------- #


def _render_index(config: dict[str, Any]) -> str:
    """Render the single-page UI, with server-driven options (languages, model)."""
    languages = config.get("languages", {})
    source_langs = languages.get("source", [])
    target_langs = languages.get("target", [])
    max_files = config.get("max_files", 10)
    max_file_mb = config.get("max_file_mb", 20)
    provider = config.get("provider", "mock")
    model = config.get("model", "")
    models = config.get("models") or ([model] if model else [])

    source_options = "\n".join(f'<option value="{lang}">{lang}</option>' for lang in source_langs)
    target_options = "\n".join(f'<option value="{lang}">{lang}</option>' for lang in target_langs)

    if provider == "gateway":
        model_options = "\n".join(
            f'<option value="{name}"{" selected" if name == model else ""}>{name}</option>'
            for name in models
        )
        model_block = f"""
        <div class="row">
          <div class="field">
            <label for="model">Model</label>
            <select id="model" name="model">
              {model_options}
            </select>
          </div>
        </div>
        """
        auth_block = """
        <div id="auth-block">
          <div id="auth-state">Checking the gateway connection...</div>
          <button type="button" id="auth-btn" hidden>Sign in</button>
          <div id="auth-code" hidden>
            Enter this code: <strong id="auth-user-code"></strong><br>
            at <a id="auth-uri" href="#" target="_blank" rel="noopener">the sign-in page</a>,
            then leave this tab open.
          </div>
        </div>
        """
    else:
        model_block = ""
        auth_block = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF Translator</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, Segoe UI, Arial, sans-serif;
    max-width: 640px;
    margin: 40px auto;
    padding: 0 16px;
    color: #1a1a1a;
    background: #fafafa;
  }}
  h1 {{ font-size: 1.4rem; margin-bottom: 4px; }}
  p.subtitle {{ color: #666; margin-top: 0; }}
  form {{
    background: #fff;
    border: 1px solid #ddd;
    border-radius: 8px;
    padding: 20px;
  }}
  #dropzone {{
    border: 2px dashed #aaa;
    border-radius: 6px;
    padding: 28px 16px;
    text-align: center;
    color: #555;
    cursor: pointer;
    margin-bottom: 16px;
  }}
  #dropzone.dragover {{ border-color: #2563eb; background: #eff6ff; }}
  #file-list {{ margin: 8px 0 16px; font-size: 0.9rem; color: #333; }}
  #file-list div {{ padding: 2px 0; }}
  .row {{ display: flex; gap: 16px; margin-bottom: 16px; }}
  .field {{ flex: 1; display: flex; flex-direction: column; gap: 4px; }}
  label {{ font-size: 0.85rem; font-weight: 600; color: #333; }}
  select {{ padding: 6px; border: 1px solid #ccc; border-radius: 4px; }}
  button {{
    background: #2563eb;
    color: #fff;
    border: none;
    padding: 10px 20px;
    border-radius: 6px;
    font-size: 1rem;
    cursor: pointer;
  }}
  button:disabled {{ background: #93b4f0; cursor: not-allowed; }}
  #progress-wrap {{ margin-top: 20px; display: none; }}
  #progress-bar-bg {{
    background: #e5e7eb;
    border-radius: 6px;
    height: 10px;
    overflow: hidden;
  }}
  #progress-bar {{
    background: #2563eb;
    height: 100%;
    width: 0%;
    transition: width 0.3s;
  }}
  #progress-message {{ font-size: 0.85rem; color: #444; margin-top: 6px; }}
  #warnings {{ margin-top: 12px; font-size: 0.85rem; color: #92400e; }}
  #error-message {{
    display: none;
    margin-top: 16px;
    padding: 10px 12px;
    background: #fef2f2;
    border: 1px solid #fecaca;
    border-radius: 6px;
    color: #991b1b;
    font-size: 0.9rem;
  }}
  #download-link {{ display: none; margin-top: 16px; }}
  #download-link a {{
    display: inline-block;
    background: #16a34a;
    color: #fff;
    text-decoration: none;
    padding: 10px 20px;
    border-radius: 6px;
  }}
  #auth-block {{
    margin-bottom: 16px;
    padding: 12px;
    border: 1px solid #e5e7eb;
    border-radius: 6px;
    background: #f9fafb;
    font-size: 0.9rem;
  }}
  #auth-state.connected {{ color: #15803d; font-weight: 600; }}
  #auth-state.disconnected {{ color: #92400e; }}
  #auth-btn {{
    margin-top: 8px;
    padding: 6px 14px;
    font-size: 0.9rem;
  }}
  #auth-code {{ margin-top: 10px; line-height: 1.6; }}
  #auth-code strong {{
    font-family: ui-monospace, Consolas, monospace;
    font-size: 1.15rem;
    letter-spacing: 2px;
    background: #fff;
    border: 1px solid #d1d5db;
    border-radius: 4px;
    padding: 2px 8px;
  }}
</style>
</head>
<body>
<h1>PDF Translator</h1>
<p class="subtitle">Translate PDF contracts — up to {max_files} files, {max_file_mb} MB each</p>

<form id="job-form">
  {auth_block}
  <div id="dropzone">
    Drop your PDFs here, or click to choose files
    <input type="file" id="file-input" name="files" accept="application/pdf" multiple hidden>
  </div>
  <div id="file-list"></div>

  <div class="row">
    <div class="field">
      <label for="src">Source language</label>
      <select id="src" name="src">
        {source_options}
      </select>
    </div>
    <div class="field">
      <label for="tgt">Target language</label>
      <select id="tgt" name="tgt">
        {target_options}
      </select>
    </div>
  </div>
  {model_block}

  <button type="submit" id="submit-btn">Translate</button>

  <div id="error-message"></div>

  <div id="progress-wrap">
    <div id="progress-bar-bg"><div id="progress-bar"></div></div>
    <div id="progress-message"></div>
    <div id="warnings"></div>
  </div>

  <div id="download-link"><a href="#" id="download-href">Download result</a></div>
</form>

<script>
const MAX_FILES = {max_files};
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('file-input');
const fileList = document.getElementById('file-list');
const form = document.getElementById('job-form');
const submitBtn = document.getElementById('submit-btn');
const errorBox = document.getElementById('error-message');
const progressWrap = document.getElementById('progress-wrap');
const progressBar = document.getElementById('progress-bar');
const progressMessage = document.getElementById('progress-message');
const warningsBox = document.getElementById('warnings');
const downloadLink = document.getElementById('download-link');
const downloadHref = document.getElementById('download-href');

let selectedFiles = [];

// --- Gateway sign-in (device-code flow) -----------------------------------
// Only rendered when provider=gateway; with the mock provider these elements
// do not exist and every function below is a no-op.
const authBlock = document.getElementById('auth-block');
const authState = document.getElementById('auth-state');
const authBtn = document.getElementById('auth-btn');
const authCode = document.getElementById('auth-code');
const authUserCode = document.getElementById('auth-user-code');
const authUri = document.getElementById('auth-uri');

let authRequired = false;
let authenticated = false;

function applyAuthGate() {{
  // The Translate button stays disabled until a gateway session exists.
  submitBtn.disabled = authRequired && !authenticated;
}}

async function refreshAuthStatus() {{
  if (!authBlock) return;
  try {{
    const res = await fetch('/auth/status');
    const data = await res.json();
    authRequired = data.required;
    authenticated = data.authenticated;

    if (authenticated) {{
      authState.textContent = 'Connected to the gateway';
      authState.className = 'connected';
      authBtn.hidden = true;
      authCode.hidden = true;
    }} else if (data.status === 'pending') {{
      authState.textContent = 'Waiting for approval in your browser...';
      authState.className = 'disconnected';
      authBtn.hidden = true;
    }} else {{
      authState.textContent = data.status === 'failed'
        ? ('Sign-in failed: ' + (data.detail || 'unknown error'))
        : 'Not connected to the gateway';
      authState.className = 'disconnected';
      authBtn.hidden = false;
      authCode.hidden = true;
    }}
    applyAuthGate();

    // Keep polling while the user approves in the other tab.
    if (authRequired && !authenticated && data.status === 'pending') {{
      setTimeout(refreshAuthStatus, 2000);
    }}
  }} catch (err) {{
    authState.textContent = 'Cannot reach the server.';
    authState.className = 'disconnected';
  }}
}}

if (authBtn) {{
  authBtn.addEventListener('click', async () => {{
    authBtn.disabled = true;
    hideError();
    try {{
      const res = await fetch('/auth/start', {{ method: 'POST' }});
      if (!res.ok) {{
        const err = await res.json().catch(() => ({{}}));
        throw new Error(err.detail || ('error ' + res.status));
      }}
      const data = await res.json();
      authUserCode.textContent = data.user_code;
      authUri.href = data.verification_uri_complete || data.verification_uri;
      authUri.textContent = data.verification_uri;
      authCode.hidden = false;
      authBtn.hidden = true;
      refreshAuthStatus();
    }} catch (err) {{
      showError(err.message);
    }} finally {{
      authBtn.disabled = false;
    }}
  }});
}}

function renderFileList() {{
  fileList.innerHTML = '';
  selectedFiles.forEach(f => {{
    const div = document.createElement('div');
    div.textContent = f.name + ' (' + (f.size / 1024 / 1024).toFixed(1) + ' MB)';
    fileList.appendChild(div);
  }});
}}

function addFiles(fileArray) {{
  selectedFiles = selectedFiles.concat(fileArray).slice(0, MAX_FILES);
  renderFileList();
}}

dropzone.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', e => addFiles(Array.from(e.target.files)));

dropzone.addEventListener('dragover', e => {{
  e.preventDefault();
  dropzone.classList.add('dragover');
}});
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
dropzone.addEventListener('drop', e => {{
  e.preventDefault();
  dropzone.classList.remove('dragover');
  addFiles(Array.from(e.dataTransfer.files));
}});

function showError(message) {{
  errorBox.textContent = message;
  errorBox.style.display = 'block';
}}
function hideError() {{
  errorBox.style.display = 'none';
  errorBox.textContent = '';
}}

let pollTimer = null;

async function pollStatus(jobId) {{
  try {{
    const res = await fetch('/jobs/' + jobId);
    if (!res.ok) {{
      throw new Error('job not found');
    }}
    const data = await res.json();
    progressMessage.textContent = data.message || '';

    if (data.status === 'running' || data.status === 'pending') {{
      progressBar.style.width = '50%';
      pollTimer = setTimeout(() => pollStatus(jobId), 2000);
    }} else if (data.status === 'done') {{
      progressBar.style.width = '100%';
      applyAuthGate();
      if (data.warnings && data.warnings.length) {{
        warningsBox.textContent = 'Warnings: ' + data.warnings.join('; ');
      }}
      downloadHref.href = '/jobs/' + jobId + '/download';
      downloadLink.style.display = 'block';
    }} else if (data.status === 'error') {{
      applyAuthGate();
      showError(data.message || "The job failed.");
    }}
  }} catch (err) {{
    applyAuthGate();
    showError("Error while tracking the job: " + err.message);
  }}
}}

form.addEventListener('submit', async e => {{
  e.preventDefault();
  hideError();
  downloadLink.style.display = 'none';
  warningsBox.textContent = '';

  if (authRequired && !authenticated) {{
    showError("Sign in to the gateway before translating.");
    return;
  }}
  if (selectedFiles.length === 0) {{
    showError("Select at least one PDF file.");
    return;
  }}
  if (selectedFiles.length > MAX_FILES) {{
    showError("Too many files (max " + MAX_FILES + ").");
    return;
  }}

  const formData = new FormData();
  selectedFiles.forEach(f => formData.append('files', f));
  formData.append('src', document.getElementById('src').value);
  formData.append('tgt', document.getElementById('tgt').value);

  submitBtn.disabled = true;
  progressWrap.style.display = 'block';
  progressBar.style.width = '10%';
  progressMessage.textContent = 'Uploading files...';

  try {{
    const res = await fetch('/jobs', {{ method: 'POST', body: formData }});
    if (!res.ok) {{
      const err = await res.json().catch(() => ({{}}));
      throw new Error(err.detail || ('error ' + res.status));
    }}
    const data = await res.json();
    pollStatus(data.job_id);
  }} catch (err) {{
    applyAuthGate();
    showError(err.message);
  }}
}});

// Initial gate: with provider=gateway the Translate button stays disabled
// until /auth/status confirms a session.
refreshAuthStatus();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Serve the single-page UI."""
    config = app_config.load_config()
    return _render_index(config)


# --------------------------------------------------------------------------- #
# Authentication (provider: gateway only)
# --------------------------------------------------------------------------- #


@app.get("/auth/status")
def auth_status() -> dict[str, Any]:
    """Report whether a gateway session is active. Never returns the token itself."""
    provider = app_config.load_config().get("provider", "mock")
    authenticated, expires_in = auth.is_authenticated()
    status, detail = auth.flow_status()

    return {
        "provider": provider,
        # With provider: mock nothing needs signing in, so the UI hides the block.
        "required": provider == "gateway",
        "authenticated": authenticated,
        "expires_in": expires_in,
        "status": status,
        "detail": detail,
    }


@app.post("/auth/start")
def auth_start() -> dict[str, Any]:
    """Begin the device-code flow and return the code the user must approve."""
    try:
        device = auth.start_device_flow()
    except auth.AuthError as error:
        # Misconfigured .env is the common case here; surface it so it can be fixed.
        raise HTTPException(status_code=503, detail=str(error)) from error

    logger.info("device-code flow started")
    return {
        "user_code": device.user_code,
        "verification_uri": device.verification_uri,
        "verification_uri_complete": device.verification_uri_complete,
        "expires_in": device.expires_in,
    }


# --------------------------------------------------------------------------- #
# Job submission
# --------------------------------------------------------------------------- #


def _execute_job(job_id: str, files: list[Path], src: str, tgt: str, config: dict) -> None:
    """Run the pipeline for a job in the background, updating the registry."""
    started = time.monotonic()
    with _jobs_lock:
        _jobs[job_id].status = "running"
        _jobs[job_id].message = "starting"

    def progress_cb(message: str) -> None:
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id].message = message

    try:
        # The web job_id is passed through so output/<job_id> is the directory
        # this module's purge logic (download, TTL, failure) actually removes.
        result = run_job(files, src, tgt, config, progress_cb=progress_cb, job_id=job_id)
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id].status = "done"
                _jobs[job_id].message = "done"
                _jobs[job_id].result = result
        duration = time.monotonic() - started
        logger.info(
            "job %s done: %d file(s), %.1fs", job_id, len(files), duration
        )
    except Exception as error:  # noqa: BLE001 - job failure must not crash the worker
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id].status = "error"
                _jobs[job_id].message = "processing failed"
                _jobs[job_id].error = type(error).__name__
        logger.warning("job %s failed: %s", job_id, type(error).__name__)
        # A failed job must leave no partial output behind.
        shutil.rmtree(OUTPUT_DIR / job_id, ignore_errors=True)
    finally:
        shutil.rmtree(TMP_DIR / job_id, ignore_errors=True)


@app.post("/jobs", status_code=202)
async def create_job(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    src: str = Form(...),
    tgt: str = Form(...),
) -> JSONResponse:
    """Validate uploaded files, persist them, and schedule the translation job."""
    _purge_expired_jobs()

    config = dict(app_config.load_config())
    languages = config.get("languages", {})
    max_files = int(config.get("max_files", 10))

    if not files:
        raise HTTPException(status_code=400, detail="No file provided.")
    if len(files) > max_files:
        raise HTTPException(
            status_code=400, detail=f"Too many files ({len(files)} > {max_files})."
        )
    if src not in languages.get("source", []):
        raise HTTPException(status_code=400, detail="Invalid source language.")
    if tgt not in languages.get("target", []):
        raise HTTPException(status_code=400, detail="Invalid target language.")

    job_id = uuid.uuid4().hex[:12]
    job_tmp_dir = TMP_DIR / job_id

    try:
        job_tmp_dir.mkdir(parents=True, exist_ok=False)
        saved_paths: list[Path] = []
        for upload in files:
            dest = job_tmp_dir / Path(upload.filename or "document.pdf").name
            content = await upload.read()
            dest.write_bytes(content)
            saved_paths.append(dest)

        for index, path in enumerate(saved_paths, start=1):
            try:
                _validate_file(path, config)
            except ValueError as error:
                # _validate_file's message starts with the user's file name;
                # error responses must not echo it back, so it is replaced by
                # the file's position in the upload.
                reason = str(error).removeprefix(f"{path.name}: ")
                raise ValueError(f"file {index}: {reason}") from error
    except ValueError as error:
        shutil.rmtree(job_tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:  # noqa: BLE001 - any save failure must not leave partial state
        shutil.rmtree(job_tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Failed to save the uploaded files.") from error

    with _jobs_lock:
        _jobs[job_id] = JobState(job_id=job_id, status="pending", message="queued")

    logger.info("job %s created: %d file(s)", job_id, len(saved_paths))
    background_tasks.add_task(_execute_job, job_id, saved_paths, src, tgt, config)

    return JSONResponse(status_code=202, content={"job_id": job_id})


# --------------------------------------------------------------------------- #
# Job status
# --------------------------------------------------------------------------- #


@app.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> dict[str, Any]:
    """Return the current status, latest progress message, and stats/warnings if done."""
    _purge_expired_jobs()

    with _jobs_lock:
        state = _jobs.get(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        payload: dict[str, Any] = {
            "job_id": state.job_id,
            "status": state.status,
            "message": state.message,
        }
        if state.status == "done" and state.result is not None:
            payload["warnings"] = state.result.warnings
            payload["stats"] = state.result.stats.summary()
        elif state.status == "error":
            payload["error"] = state.error

        return payload


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


@app.get("/jobs/{job_id}/download")
def download_job(job_id: str) -> StreamingResponse:
    """Stream a zip of the translated PDFs, then purge the job's directories."""
    _purge_expired_jobs()

    with _jobs_lock:
        state = _jobs.get(job_id)
        if state is None or state.status != "done" or state.result is None:
            raise HTTPException(status_code=404, detail="Job not found or not finished yet.")
        result = state.result

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_result in result.files:
            if file_result.output_path is None:
                continue
            output_path = Path(file_result.output_path)
            if output_path.is_file():
                archive.write(output_path, arcname=output_path.name)
    buffer.seek(0)

    with _jobs_lock:
        _jobs.pop(job_id, None)
    _cleanup_job_dirs(job_id)
    logger.info("job %s downloaded and purged", job_id)

    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=translations_{job_id}.zip"},
    )
