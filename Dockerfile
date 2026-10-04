# Multi-stage image: dependencies are installed from the hashed lock file in a
# builder stage; the runtime stage ships only the virtualenv and the app code.
# Both stages use the same base so the copied venv's interpreter links resolve.
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d

FROM ${PYTHON_IMAGE} AS builder
RUN python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
RUN /opt/venv/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.lock

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

# CJK fonts for rendering translated pages.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Fixed numeric IDs so orchestrators can verify the user is not root.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
# Root-owned code (read-only for the app user). The .dockerignore allow-list
# limits the build context to app/, prompts/, data/ and config.yaml.
COPY . /app
# The only writable paths: scratch files, results and LLM traces.
RUN mkdir -p /app/tmp /app/output /app/traces \
    && chown app:app /app/tmp /app/output /app/traces

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

# The job registry is in memory: run exactly one worker.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
