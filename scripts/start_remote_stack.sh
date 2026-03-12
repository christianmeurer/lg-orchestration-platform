#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export LG_REPO_ROOT="${LG_REPO_ROOT:-$ROOT_DIR}"
export LG_PROFILE="${LG_PROFILE:-prod}"

RUNNER_BIND="${LG_RUNNER_BIND:-127.0.0.1:8088}"
RUNNER_HEALTH_URL="${LG_RUNNER_HEALTH_URL:-http://127.0.0.1:8088/healthz}"
REMOTE_API_HOST="${LG_REMOTE_API_HOST:-0.0.0.0}"
REMOTE_API_PORT="${LG_REMOTE_API_PORT:-${PORT:-${WEBSITES_PORT:-8001}}}"

cleanup() {
  if [[ -n "${api_pid:-}" ]] && kill -0 "$api_pid" 2>/dev/null; then
    kill "$api_pid" 2>/dev/null || true
  fi
  if [[ -n "${runner_pid:-}" ]] && kill -0 "$runner_pid" 2>/dev/null; then
    kill "$runner_pid" 2>/dev/null || true
  fi
  wait "${api_pid:-}" 2>/dev/null || true
  wait "${runner_pid:-}" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

echo "[startup] repo root: ${LG_REPO_ROOT}"
echo "[startup] profile: ${LG_PROFILE}"
echo "[startup] runner bind: ${RUNNER_BIND}"
echo "[startup] remote api: http://${REMOTE_API_HOST}:${REMOTE_API_PORT}"

runner_cmd=(
  "$ROOT_DIR/rs/target/release/lg-runner"
  --bind "$RUNNER_BIND"
  --root-dir "$LG_REPO_ROOT"
  --profile "$LG_PROFILE"
)
if [[ -n "${LG_RUNNER_API_KEY:-}" ]]; then
  runner_cmd+=(--api-key "$LG_RUNNER_API_KEY")
fi

"${runner_cmd[@]}" &
runner_pid=$!

for _ in $(seq 1 60); do
  if curl -fsS "$RUNNER_HEALTH_URL" > /dev/null; then
    break
  fi
  sleep 0.5
done

if ! curl -fsS "$RUNNER_HEALTH_URL" > /dev/null; then
  echo "[startup] runner did not become healthy: ${RUNNER_HEALTH_URL}" >&2
  exit 1
fi

(
  cd "$ROOT_DIR/py"
  uv run --project . lg-orch serve-api --host "$REMOTE_API_HOST" --port "$REMOTE_API_PORT"
) &
api_pid=$!

wait -n "$runner_pid" "$api_pid"
exit $?
