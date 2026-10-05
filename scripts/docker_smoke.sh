#!/usr/bin/env bash
# Smoke test of the container image: health, access guard, end-to-end job.
# Usage: bash scripts/docker_smoke.sh [image]   (default: pdf-translator:ci)
# Needs docker, curl and python3. The password is generated at run time.
set -euo pipefail

IMAGE="${1:-pdf-translator:ci}"
NAME="app"
BASE="http://127.0.0.1:8000"
FIXTURE="tests/fixtures/simple.pdf"
WORK="$(mktemp -d)"

APP_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  echo "::add-mask::${APP_PASSWORD}"
fi
export APP_PASSWORD

cleanup() {
  status=$?
  if [ "${status}" -ne 0 ]; then
    echo "smoke test failed, container logs:"
    docker logs "${NAME}" 2>&1 | tail -n 100 || true
  fi
  docker rm -f "${NAME}" >/dev/null 2>&1 || true
  rm -rf "${WORK}"
  exit "${status}"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $1"
  exit 1
}

docker rm -f "${NAME}" >/dev/null 2>&1 || true
docker run -d --name "${NAME}" -p 127.0.0.1:8000:8000 \
  -e APP_PASSWORD -e APP_PROVIDER=fake "${IMAGE}" >/dev/null

echo "waiting for /healthz"
ready=0
for _ in $(seq 1 60); do
  if curl -fsS "${BASE}/healthz" -o "${WORK}/health.json" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
[ "${ready}" -eq 1 ] || fail "/healthz not reachable within 60 s"
python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])) == {"status": "ok"} else 1)' \
  "${WORK}/health.json" || fail "unexpected /healthz body"

code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/")
[ "${code}" = "401" ] || fail "expected 401 without credentials, got ${code}"
code=$(curl -s -o /dev/null -w '%{http_code}' -u "ci:${APP_PASSWORD}" "${BASE}/")
[ "${code}" = "200" ] || fail "expected 200 with credentials, got ${code}"

echo "end-to-end job"
curl -fsS -u "ci:${APP_PASSWORD}" -F "files=@${FIXTURE}" -F "src=zh" -F "tgt=en" \
  "${BASE}/jobs" -o "${WORK}/job.json"
job_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["job_id"])' "${WORK}/job.json")

state=""
for _ in $(seq 1 90); do
  curl -fsS -u "ci:${APP_PASSWORD}" "${BASE}/jobs/${job_id}" -o "${WORK}/status.json"
  state=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status", ""))' "${WORK}/status.json")
  case "${state}" in
    done | error | failed) break ;;
  esac
  sleep 1
done
[ "${state}" = "done" ] || fail "job did not complete (last status: ${state})"

curl -fsS -u "ci:${APP_PASSWORD}" "${BASE}/jobs/${job_id}/download" -o "${WORK}/result.zip"
python3 -c 'import sys, zipfile; z = zipfile.ZipFile(sys.argv[1]); sys.exit(0 if z.namelist() else 1)' \
  "${WORK}/result.zip" || fail "download is not a non-empty zip"

echo "waiting for the container healthcheck"
health=""
for _ in $(seq 1 40); do
  health=$(docker inspect -f '{{.State.Health.Status}}' "${NAME}")
  [ "${health}" = "healthy" ] && break
  sleep 3
done
[ "${health}" = "healthy" ] || fail "container health is '${health}'"

echo "smoke test passed"
