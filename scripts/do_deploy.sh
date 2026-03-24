#!/usr/bin/env bash
# do_deploy.sh — Build, push to DOCR, and deploy to DigitalOcean App Platform or Droplet.
# See docs/deployment_digitalocean.md for full documentation.
set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve repo root relative to this script
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
IMAGE_TAG="${1:-latest}"
DO_REGISTRY="${DO_REGISTRY:-}"
DO_APP_NAME="${DO_APP_NAME:-lula-orch}"
DO_REGION="${DO_REGION:-nyc3}"
DO_DEPLOY_TARGET="${DO_DEPLOY_TARGET:-app}"
APP_PORT="${PORT:-8001}"
LG_PROFILE="${LG_PROFILE:-prod}"
DO_DROPLET_SSH_KEY="${DO_DROPLET_SSH_KEY:-}"

# App Platform spec path
APP_SPEC="${ROOT_DIR}/infra/do/app.yaml"

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
  cat >&2 <<EOF
Usage: DO_REGISTRY=<name> [options] bash scripts/do_deploy.sh [image-tag]

Required environment variables:
  DO_REGISTRY                   DOCR registry name (e.g. lula-orch)

Optional environment variables:
  DO_APP_NAME                   App / image name            (default: lula-orch)
  DO_REGION                     DigitalOcean region slug    (default: nyc3)
  DO_DEPLOY_TARGET              app | droplet               (default: app)
  DO_DROPLET_SSH_KEY            SSH key fingerprint or ID   (droplet target only)
  PORT                          Remote API port             (default: 8001)
  LG_PROFILE                    Config profile              (default: prod)
  LG_REMOTE_API_AUTH_MODE       bearer | off                (default: bearer if token present)
  LG_REMOTE_API_BEARER_TOKEN    Bearer token (secret)
  LG_REMOTE_API_TRUST_FORWARDED_HEADERS  true | false
  LG_RUNNER_API_KEY             Runner ↔ orchestrator key   (secret)
  MODEL_ACCESS_KEY              Generic model key           (secret)
  DIGITAL_OCEAN_MODEL_ACCESS_KEY  DO GenAI key              (secret)
  LG_CHECKPOINT_REDIS_URL       Valkey/Redis checkpoint URI (secret)

App Platform note:
  Secret env keys are declared in infra/do/app.yaml, but values are not
  populated automatically for App Platform. Set them via the DO console or with:
     doctl apps update <APP_ID> --spec infra/do/app.yaml
  then edit these secrets in the DO console:
    LG_REMOTE_API_BEARER_TOKEN
    LG_RUNNER_API_KEY
    DIGITAL_OCEAN_MODEL_ACCESS_KEY
    MODEL_ACCESS_KEY
    LG_CHECKPOINT_REDIS_URL
EOF
  exit 1
}

ensure_app_platform_secret_key() {
  local spec_path="$1"
  local secret_key="$2"

  if grep -q "key: ${secret_key}" "${spec_path}"; then
    return 0
  fi

  python3 - "${spec_path}" "${secret_key}" <<'PYEOF'
from pathlib import Path
import sys

spec_path = Path(sys.argv[1])
secret_key = sys.argv[2]
text = spec_path.read_text()

anchor = "      - key: MODEL_ACCESS_KEY\n        scope: RUN_TIME\n        type: SECRET\n"
insert = anchor + f"      - key: {secret_key}\n        scope: RUN_TIME\n        type: SECRET\n"

if anchor in text:
    text = text.replace(anchor, insert, 1)
spec_path.write_text(text)
PYEOF
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
if [[ -z "${DO_REGISTRY}" ]]; then
  echo "[error] DO_REGISTRY is required" >&2
  usage
fi

if [[ "${DO_DEPLOY_TARGET}" != "app" && "${DO_DEPLOY_TARGET}" != "droplet" ]]; then
  echo "[error] DO_DEPLOY_TARGET must be 'app' or 'droplet', got: ${DO_DEPLOY_TARGET}" >&2
  usage
fi

FULL_IMAGE="registry.digitalocean.com/${DO_REGISTRY}/${DO_APP_NAME}:${IMAGE_TAG}"

# Derive auth mode default
if [[ -z "${LG_REMOTE_API_AUTH_MODE:-}" ]]; then
  if [[ -n "${LG_REMOTE_API_BEARER_TOKEN:-}" ]]; then
    LG_REMOTE_API_AUTH_MODE="bearer"
  else
    LG_REMOTE_API_AUTH_MODE="off"
  fi
fi

# Derive forwarded headers default
if [[ -z "${LG_REMOTE_API_TRUST_FORWARDED_HEADERS:-}" ]]; then
  if [[ "${DO_DEPLOY_TARGET}" == "app" ]]; then
    LG_REMOTE_API_TRUST_FORWARDED_HEADERS="true"
  else
    LG_REMOTE_API_TRUST_FORWARDED_HEADERS="false"
  fi
fi

echo "[deploy] root:   ${ROOT_DIR}"
echo "[deploy] target: ${DO_DEPLOY_TARGET}"
echo "[deploy] image:  ${FULL_IMAGE}"

# ---------------------------------------------------------------------------
# Step 1 — ensure DOCR registry exists
# ---------------------------------------------------------------------------
echo "[deploy] ensuring DOCR registry '${DO_REGISTRY}' exists..."
if ! doctl registry get "${DO_REGISTRY}" > /dev/null 2>&1; then
  echo "[deploy] creating registry ${DO_REGISTRY} in ${DO_REGION}"
  doctl registry create "${DO_REGISTRY}" --region "${DO_REGION}"
fi

# ---------------------------------------------------------------------------
# Step 2 — authenticate Docker to DOCR
# ---------------------------------------------------------------------------
echo "[deploy] authenticating Docker to DOCR..."
doctl registry login

# ---------------------------------------------------------------------------
# Step 3 — build and push
# ---------------------------------------------------------------------------
echo "[deploy] building ${FULL_IMAGE}..."
docker build -t "${FULL_IMAGE}" "${ROOT_DIR}"

echo "[deploy] pushing ${FULL_IMAGE}..."
docker push "${FULL_IMAGE}"

# ---------------------------------------------------------------------------
# App Platform deploy
# ---------------------------------------------------------------------------
deploy_app_platform() {
  echo "[deploy] target: App Platform"

  PATCHED_SPEC="$(mktemp --suffix=.yaml)"
  # shellcheck disable=SC2064
  trap "rm -f '${PATCHED_SPEC}'" EXIT

  # Find existing app by name
  APP_ID=""
  while IFS=$'\t' read -r name id; do
    if [[ "${name}" == "${DO_APP_NAME}" ]]; then
      APP_ID="${id}"
      break
    fi
  done < <(doctl apps list --format Name,ID --no-header 2>/dev/null || true)

  if [[ -z "${APP_ID}" ]]; then
    # New app — use the static spec, no secrets to preserve
    echo "[deploy] creating new App Platform app '${DO_APP_NAME}'..."
    sed \
      -e "s|registry: .*|registry: ${DO_REGISTRY}|" \
      -e "s|repository: .*|repository: ${DO_APP_NAME}|" \
      -e "s|tag: .*|tag: ${IMAGE_TAG}|" \
      "${APP_SPEC}" > "${PATCHED_SPEC}"

    ensure_app_platform_secret_key "${PATCHED_SPEC}" "LG_CHECKPOINT_REDIS_URL"

    CREATE_OUTPUT="$(doctl apps create --spec "${PATCHED_SPEC}" --format ID --no-header 2>/dev/null || true)"
    APP_ID="${CREATE_OUTPUT//[[:space:]]/}"

    if [[ -z "${APP_ID}" ]]; then
      # Fallback: poll the list with retries to handle DO API propagation delay
      echo "[deploy] waiting for app '${DO_APP_NAME}' to appear in app list..."
      for _retry in $(seq 1 12); do
        while IFS=$'\t' read -r name id; do
          if [[ "${name}" == "${DO_APP_NAME}" ]]; then
            APP_ID="${id}"
            break 2
          fi
        done < <(doctl apps list --format Name,ID --no-header 2>/dev/null || true)
        if [[ -n "${APP_ID}" ]]; then
          break
        fi
        sleep 5
      done
    fi
  else
    # Existing app — fetch the live spec so EV[...] secret refs are preserved,
    # then patch only image tag and non-secret env vars.
    echo "[deploy] updating existing app '${DO_APP_NAME}' (${APP_ID})..."
    doctl apps spec get "${APP_ID}" > "${PATCHED_SPEC}"
    # Patch image tag in the live spec
    sed -i \
      -e "s|registry: .*|registry: ${DO_REGISTRY}|" \
      -e "s|repository: .*|repository: ${DO_APP_NAME}|" \
      -e "s|tag: .*|tag: ${IMAGE_TAG}|" \
      "${PATCHED_SPEC}"

    ensure_app_platform_secret_key "${PATCHED_SPEC}" "LG_CHECKPOINT_REDIS_URL"

    doctl apps update "${APP_ID}" --spec "${PATCHED_SPEC}"
  fi

  if [[ -z "${APP_ID}" ]]; then
    echo "[deploy] warning: could not determine APP_ID; check the DO console" >&2
    return
  fi

  # Print instructions for secrets
  cat <<EOF

[deploy] NOTE — secret environment variables are NOT set automatically.
  Set them via the DO console or run:
    doctl apps update ${APP_ID} --spec infra/do/app.yaml
  and then update these secrets in the console:
    LG_REMOTE_API_BEARER_TOKEN
    LG_RUNNER_API_KEY
    DIGITAL_OCEAN_MODEL_ACCESS_KEY
    MODEL_ACCESS_KEY
    LG_CHECKPOINT_REDIS_URL
EOF

  # Wait briefly then fetch live URL
  sleep 3
  LIVE_URL="$(doctl apps get "${APP_ID}" --format LiveURL --no-header 2>/dev/null || true)"
  if [[ -n "${LIVE_URL}" ]]; then
    echo "[deploy] remote api: ${LIVE_URL}"
  else
    echo "[deploy] app is deploying; check status with: doctl apps get ${APP_ID}"
  fi
}

# ---------------------------------------------------------------------------
# Droplet deploy
# ---------------------------------------------------------------------------
deploy_droplet() {
  echo "[deploy] target: Droplet"

  DROPLET_SIZE="${DO_DROPLET_SIZE:-s-1vcpu-2gb}"
  DROPLET_IMAGE="${DO_DROPLET_OS_IMAGE:-ubuntu-22-04-x64}"

  # Find existing droplet by name
  DROPLET_ID=""
  DROPLET_IP=""
  while IFS=$'\t' read -r name id ip; do
    if [[ "${name}" == "${DO_APP_NAME}" ]]; then
      DROPLET_ID="${id}"
      DROPLET_IP="${ip}"
      break
    fi
  done < <(doctl compute droplet list --format Name,ID,PublicIPv4 --no-header 2>/dev/null || true)

  if [[ -z "${DROPLET_ID}" ]]; then
    echo "[deploy] creating Droplet '${DO_APP_NAME}' (${DROPLET_SIZE}, ${DROPLET_IMAGE})..."

    # Build cloud-init user-data that installs Docker and runs the container
    CLOUD_INIT="$(mktemp --suffix=.sh)"
    # shellcheck disable=SC2064
    trap "rm -f '${CLOUD_INIT}'" EXIT

    # Encode secrets to avoid quoting issues inside heredoc
    _b64() { printf '%s' "${1:-}" | base64; }
    _BEARER_B64="$(_b64 "${LG_REMOTE_API_BEARER_TOKEN:-}")"
    _RUNNER_KEY_B64="$(_b64 "${LG_RUNNER_API_KEY:-}")"
    _MODEL_KEY_B64="$(_b64 "${MODEL_ACCESS_KEY:-}")"
    _DO_MODEL_KEY_B64="$(_b64 "${DIGITAL_OCEAN_MODEL_ACCESS_KEY:-}")"
    _CHECKPOINT_REDIS_URL_B64="$(_b64 "${LG_CHECKPOINT_REDIS_URL:-}")"

    cat > "${CLOUD_INIT}" <<CLOUDINIT
#!/usr/bin/env sh
set -eu
APP_NAME="${DO_APP_NAME}"
APP_PORT="${APP_PORT}"
IMAGE="${FULL_IMAGE}"
LG_PROFILE="${LG_PROFILE}"
LG_REMOTE_API_AUTH_MODE="${LG_REMOTE_API_AUTH_MODE}"
LG_REMOTE_API_TRUST_FORWARDED_HEADERS="${LG_REMOTE_API_TRUST_FORWARDED_HEADERS}"
LG_REMOTE_API_BEARER_TOKEN="\$(printf '%s' '${_BEARER_B64}' | base64 -d)"
LG_RUNNER_API_KEY="\$(printf '%s' '${_RUNNER_KEY_B64}' | base64 -d)"
MODEL_ACCESS_KEY="\$(printf '%s' '${_MODEL_KEY_B64}' | base64 -d)"
DIGITAL_OCEAN_MODEL_ACCESS_KEY="\$(printf '%s' '${_DO_MODEL_KEY_B64}' | base64 -d)"
LG_CHECKPOINT_REDIS_URL="\$(printf '%s' '${_CHECKPOINT_REDIS_URL_B64}' | base64 -d)"

apt-get update -qq
apt-get install -y -qq ca-certificates curl docker.io
systemctl enable docker
systemctl start docker

# Authenticate to DOCR using a temporary doctl token already available via metadata
# The registry token is embedded via the DO API at push time; use doctl if available,
# otherwise expect the image to be public or pre-pulled.
if command -v doctl > /dev/null 2>&1; then
  doctl registry login
fi

docker pull "\${IMAGE}"
docker rm -f "\${APP_NAME}" > /dev/null 2>&1 || true

RUN_ARGS="-d --name \${APP_NAME} --restart unless-stopped -p \${APP_PORT}:\${APP_PORT}"
RUN_ARGS="\${RUN_ARGS} -e LG_PROFILE=\${LG_PROFILE}"
RUN_ARGS="\${RUN_ARGS} -e PORT=\${APP_PORT}"
RUN_ARGS="\${RUN_ARGS} -e LG_REMOTE_API_AUTH_MODE=\${LG_REMOTE_API_AUTH_MODE}"
RUN_ARGS="\${RUN_ARGS} -e LG_REMOTE_API_TRUST_FORWARDED_HEADERS=\${LG_REMOTE_API_TRUST_FORWARDED_HEADERS}"
if [ -n "\${LG_REMOTE_API_BEARER_TOKEN}" ]; then RUN_ARGS="\${RUN_ARGS} -e LG_REMOTE_API_BEARER_TOKEN=\${LG_REMOTE_API_BEARER_TOKEN}"; fi
if [ -n "\${LG_RUNNER_API_KEY}" ]; then RUN_ARGS="\${RUN_ARGS} -e LG_RUNNER_API_KEY=\${LG_RUNNER_API_KEY}"; fi
if [ -n "\${MODEL_ACCESS_KEY}" ]; then RUN_ARGS="\${RUN_ARGS} -e MODEL_ACCESS_KEY=\${MODEL_ACCESS_KEY}"; fi
if [ -n "\${DIGITAL_OCEAN_MODEL_ACCESS_KEY}" ]; then RUN_ARGS="\${RUN_ARGS} -e DIGITAL_OCEAN_MODEL_ACCESS_KEY=\${DIGITAL_OCEAN_MODEL_ACCESS_KEY}"; fi
if [ -n "\${LG_CHECKPOINT_REDIS_URL}" ]; then RUN_ARGS="\${RUN_ARGS} -e LG_CHECKPOINT_REDIS_URL=\${LG_CHECKPOINT_REDIS_URL}"; fi

# shellcheck disable=SC2086
docker run \${RUN_ARGS} "\${IMAGE}"

i=0
while [ "\${i}" -lt 60 ]; do
  if curl -fsS "http://127.0.0.1:\${APP_PORT}/healthz" > /dev/null; then
    exit 0
  fi
  i=\$((i + 1))
  sleep 2
done
echo "remote api did not become healthy on port \${APP_PORT}" >&2
docker logs "\${APP_NAME}" --tail 200 || true
exit 1
CLOUDINIT

    CREATE_ARGS=(
      --image "${DROPLET_IMAGE}"
      --size "${DROPLET_SIZE}"
      --region "${DO_REGION}"
      --user-data-file "${CLOUD_INIT}"
    )
    if [[ -n "${DO_DROPLET_SSH_KEY}" ]]; then
      CREATE_ARGS+=(--ssh-keys "${DO_DROPLET_SSH_KEY}")
    fi

    DROPLET_ID="$(
      doctl compute droplet create "${DO_APP_NAME}" "${CREATE_ARGS[@]}" \
        --format ID --no-header --wait
    )"

    echo "[deploy] Droplet created: ${DROPLET_ID}"

    # Wait for public IP
    for _ in $(seq 1 30); do
      DROPLET_IP="$(doctl compute droplet get "${DROPLET_ID}" --format PublicIPv4 --no-header 2>/dev/null || true)"
      if [[ -n "${DROPLET_IP}" && "${DROPLET_IP}" != "<nil>" ]]; then
        break
      fi
      sleep 3
    done

    # Open the API port in the Droplet's firewall
    doctl compute firewall list --format Name,ID --no-header 2>/dev/null | while IFS=$'\t' read -r fw_name fw_id; do
      if [[ "${fw_name}" == "${DO_APP_NAME}-fw" ]]; then
        echo "[deploy] firewall '${DO_APP_NAME}-fw' already exists (${fw_id})"
        exit 0
      fi
    done
    doctl compute firewall create \
      --name "${DO_APP_NAME}-fw" \
      --droplet-ids "${DROPLET_ID}" \
      --inbound-rules "protocol:tcp,ports:${APP_PORT},address:0.0.0.0/0,address:::/0" \
      --outbound-rules "protocol:tcp,ports:all,address:0.0.0.0/0 protocol:udp,ports:all,address:0.0.0.0/0 protocol:icmp,address:0.0.0.0/0" \
      2>/dev/null || true

  else
    echo "[deploy] Droplet '${DO_APP_NAME}' exists (${DROPLET_ID}), updating container..."

    if [[ -z "${DO_DROPLET_SSH_KEY}" ]]; then
      echo "[deploy] warning: DO_DROPLET_SSH_KEY not set; attempting SSH with default key" >&2
    fi

    SSH_OPTS=(-o StrictHostKeyChecking=no -o BatchMode=yes)

    _b64() { printf '%s' "${1:-}" | base64; }
    _BEARER_B64="$(_b64 "${LG_REMOTE_API_BEARER_TOKEN:-}")"
    _RUNNER_KEY_B64="$(_b64 "${LG_RUNNER_API_KEY:-}")"
    _MODEL_KEY_B64="$(_b64 "${MODEL_ACCESS_KEY:-}")"
    _DO_MODEL_KEY_B64="$(_b64 "${DIGITAL_OCEAN_MODEL_ACCESS_KEY:-}")"
    _CHECKPOINT_REDIS_URL_B64="$(_b64 "${LG_CHECKPOINT_REDIS_URL:-}")"

    # shellcheck disable=SC2029
    ssh "${SSH_OPTS[@]}" "root@${DROPLET_IP}" "
set -eu
APP_NAME='${DO_APP_NAME}'
APP_PORT='${APP_PORT}'
IMAGE='${FULL_IMAGE}'
LG_PROFILE='${LG_PROFILE}'
LG_REMOTE_API_AUTH_MODE='${LG_REMOTE_API_AUTH_MODE}'
LG_REMOTE_API_TRUST_FORWARDED_HEADERS='${LG_REMOTE_API_TRUST_FORWARDED_HEADERS}'
LG_REMOTE_API_BEARER_TOKEN=\"\$(printf '%s' '${_BEARER_B64}' | base64 -d)\"
LG_RUNNER_API_KEY=\"\$(printf '%s' '${_RUNNER_KEY_B64}' | base64 -d)\"
MODEL_ACCESS_KEY=\"\$(printf '%s' '${_MODEL_KEY_B64}' | base64 -d)\"
DIGITAL_OCEAN_MODEL_ACCESS_KEY=\"\$(printf '%s' '${_DO_MODEL_KEY_B64}' | base64 -d)\"
LG_CHECKPOINT_REDIS_URL=\"\$(printf '%s' '${_CHECKPOINT_REDIS_URL_B64}' | base64 -d)\"

docker pull \"\${IMAGE}\"
docker rm -f \"\${APP_NAME}\" > /dev/null 2>&1 || true

RUN_ARGS=\"-d --name \${APP_NAME} --restart unless-stopped -p \${APP_PORT}:\${APP_PORT}\"
RUN_ARGS=\"\${RUN_ARGS} -e LG_PROFILE=\${LG_PROFILE}\"
RUN_ARGS=\"\${RUN_ARGS} -e PORT=\${APP_PORT}\"
RUN_ARGS=\"\${RUN_ARGS} -e LG_REMOTE_API_AUTH_MODE=\${LG_REMOTE_API_AUTH_MODE}\"
RUN_ARGS=\"\${RUN_ARGS} -e LG_REMOTE_API_TRUST_FORWARDED_HEADERS=\${LG_REMOTE_API_TRUST_FORWARDED_HEADERS}\"
if [ -n \"\${LG_REMOTE_API_BEARER_TOKEN}\" ]; then RUN_ARGS=\"\${RUN_ARGS} -e LG_REMOTE_API_BEARER_TOKEN=\${LG_REMOTE_API_BEARER_TOKEN}\"; fi
if [ -n \"\${LG_RUNNER_API_KEY}\" ]; then RUN_ARGS=\"\${RUN_ARGS} -e LG_RUNNER_API_KEY=\${LG_RUNNER_API_KEY}\"; fi
if [ -n \"\${MODEL_ACCESS_KEY}\" ]; then RUN_ARGS=\"\${RUN_ARGS} -e MODEL_ACCESS_KEY=\${MODEL_ACCESS_KEY}\"; fi
if [ -n \"\${DIGITAL_OCEAN_MODEL_ACCESS_KEY}\" ]; then RUN_ARGS=\"\${RUN_ARGS} -e DIGITAL_OCEAN_MODEL_ACCESS_KEY=\${DIGITAL_OCEAN_MODEL_ACCESS_KEY}\"; fi
if [ -n \"\${LG_CHECKPOINT_REDIS_URL}\" ]; then RUN_ARGS=\"\${RUN_ARGS} -e LG_CHECKPOINT_REDIS_URL=\${LG_CHECKPOINT_REDIS_URL}\"; fi

docker run \${RUN_ARGS} \"\${IMAGE}\"

i=0
while [ \"\${i}\" -lt 60 ]; do
  if curl -fsS \"http://127.0.0.1:\${APP_PORT}/healthz\" > /dev/null; then
    exit 0
  fi
  i=\$((i + 1))
  sleep 2
done
echo \"remote api did not become healthy on port \${APP_PORT}\" >&2
docker logs \"\${APP_NAME}\" --tail 200 || true
exit 1
"
  fi

  echo "[deploy] remote api: http://${DROPLET_IP}:${APP_PORT}"
  echo "[deploy] warning: Droplet mode exposes HTTP directly. Add TLS (nginx + certbot) before public internet use."
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "${DO_DEPLOY_TARGET}" in
  app)      deploy_app_platform ;;
  droplet)  deploy_droplet ;;
esac
