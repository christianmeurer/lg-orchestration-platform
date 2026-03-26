#!/usr/bin/env bash
# do_deploy_one_shot.sh — opinionated DigitalOcean deployment wrapper.
#
# What it does:
#   1. Generates Lula runtime secrets when missing.
#   2. Discovers available DO Gradient models and selects planner/router defaults.
#   3. Builds and deploys the App Platform service via scripts/do_deploy.sh.
#   4. Injects both secret and non-secret env vars into the live App Platform spec.
#   5. Optionally deploys the split DOKS runner topology via scripts/do_deploy_k8s.sh.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: DO_REGISTRY=<registry> DIGITAL_OCEAN_MODEL_ACCESS_KEY=<key> \
  bash scripts/do_deploy_one_shot.sh [IMAGE_TAG]

Required environment variables:
  DO_REGISTRY                         DOCR registry name (e.g. lula-orch)
  DIGITAL_OCEAN_MODEL_ACCESS_KEY      Existing DO Gradient model access key

Optional environment variables:
  DO_APP_NAME                         App Platform app name (default: lula-orch)
  DO_REGION                           DigitalOcean region (default: nyc3)
  DEPLOY_STACK                        app | split (default: app)
  DO_CLUSTER_NAME                     DOKS cluster name for split mode
  DO_K8S_VERSION                      Kubernetes version for split mode
  LG_CHECKPOINT_REDIS_URL             Valkey/Redis URI
  LG_REMOTE_API_BEARER_TOKEN          Pre-set API bearer token; generated if absent
  LG_RUNNER_API_KEY                   Pre-set runner API key; generated if absent
  MODEL_ACCESS_KEY                    Optional generic provider key; defaults to DIGITAL_OCEAN_MODEL_ACCESS_KEY
  LG_PLANNER_MODEL                    Override planner model
  LG_ROUTER_MODEL                     Override router model
EOF
  exit 1
}

require_cmd() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "[error] required command not found: $name" >&2
    exit 1
  fi
}

generate_hex_secret() {
  python - <<'PYEOF'
import secrets
print(secrets.token_hex(32))
PYEOF
}

if [[ -z "${DO_REGISTRY:-}" || -z "${DIGITAL_OCEAN_MODEL_ACCESS_KEY:-}" ]]; then
  usage
fi

require_cmd doctl
require_cmd docker
require_cmd python
require_cmd git

DO_APP_NAME="${DO_APP_NAME:-lula-orch}"
DO_REGION="${DO_REGION:-nyc3}"
DEPLOY_STACK="${DEPLOY_STACK:-app}"
IMAGE_TAG="${1:-$(git rev-parse --short HEAD)}"

if [[ "${DEPLOY_STACK}" != "app" && "${DEPLOY_STACK}" != "split" ]]; then
  echo "[error] DEPLOY_STACK must be 'app' or 'split'" >&2
  exit 1
fi

LG_REMOTE_API_BEARER_TOKEN="${LG_REMOTE_API_BEARER_TOKEN:-$(generate_hex_secret)}"
LG_RUNNER_API_KEY="${LG_RUNNER_API_KEY:-$(generate_hex_secret)}"
MODEL_ACCESS_KEY="${MODEL_ACCESS_KEY:-${DIGITAL_OCEAN_MODEL_ACCESS_KEY}}"
LG_PROFILE="${LG_PROFILE:-prod}"

discover_model_selection() {
  local models_json
  models_json="$(doctl -o json gradient list-models)"
  python - "$models_json" <<'PYEOF'
import json
import sys

models = json.loads(sys.argv[1])
names = [str(item.get("name") or item.get("Name") or "").strip() for item in models]
lower_names = [name.lower() for name in names if name]

planner_candidates = [
    ("openai gpt-5.4", "openai-gpt-5.4"),
    ("openai gpt-5.2", "openai-gpt-5.2"),
    ("openai gpt-5", "openai-gpt-5"),
    ("anthropic claude sonnet 4.6", "anthropic-claude-sonnet-4-6"),
    ("openai gpt-4.1", "openai-gpt-4.1"),
]
router_candidates = [
    ("openai gpt-4o mini", "openai-gpt-4o-mini"),
    ("anthropic claude haiku 4.5", "anthropic-claude-haiku-4-5"),
    ("openai gpt-oss-20b", "openai-gpt-oss-20b"),
    ("openai gpt-4.1", "openai-gpt-4.1"),
]

def pick(candidates, fallback):
    for needle, slug in candidates:
        if any(needle in candidate for candidate in lower_names):
            return slug
    return fallback

print(pick(planner_candidates, "openai-gpt-4.1"))
print(pick(router_candidates, "openai-gpt-4o-mini"))
PYEOF
}

if [[ -z "${LG_PLANNER_MODEL:-}" || -z "${LG_ROUTER_MODEL:-}" ]]; then
  mapfile -t _DISCOVERED_MODELS < <(discover_model_selection)
  LG_PLANNER_MODEL="${LG_PLANNER_MODEL:-${_DISCOVERED_MODELS[0]:-openai-gpt-4.1}}"
  LG_ROUTER_MODEL="${LG_ROUTER_MODEL:-${_DISCOVERED_MODELS[1]:-openai-gpt-4o-mini}}"
fi

echo "[deploy-one-shot] stack:   ${DEPLOY_STACK}"
echo "[deploy-one-shot] region:  ${DO_REGION}"
echo "[deploy-one-shot] app:     ${DO_APP_NAME}"
echo "[deploy-one-shot] image:   ${IMAGE_TAG}"
echo "[deploy-one-shot] planner: ${LG_PLANNER_MODEL}"
echo "[deploy-one-shot] router:  ${LG_ROUTER_MODEL}"

export DO_REGISTRY
export DO_APP_NAME
export DO_REGION
export LG_PROFILE
export LG_REMOTE_API_AUTH_MODE="bearer"
export LG_REMOTE_API_TRUST_FORWARDED_HEADERS="true"
export LG_REMOTE_API_BEARER_TOKEN
export LG_RUNNER_API_KEY
export DIGITAL_OCEAN_MODEL_ACCESS_KEY
export MODEL_ACCESS_KEY
export LG_PLANNER_PROVIDER="digitalocean"
export LG_PLANNER_MODEL
export LG_ROUTER_PROVIDER="digitalocean"
export LG_ROUTER_MODEL

bash scripts/do_deploy.sh "${IMAGE_TAG}"

APP_ID="$(doctl apps list --format Spec.Name,ID --no-header | python - "${DO_APP_NAME}" <<'PYEOF'
import sys
target = sys.argv[1]
for raw in sys.stdin:
    parts = raw.rstrip("\n").split("\t")
    if len(parts) >= 2 and parts[0] == target:
        print(parts[1])
        break
PYEOF
)"

if [[ -z "${APP_ID}" ]]; then
  echo "[error] failed to resolve App Platform app ID for ${DO_APP_NAME}" >&2
  exit 1
fi

PATCHED_SPEC="$(mktemp --suffix=.yaml)"
trap 'rm -f "${PATCHED_SPEC}"' EXIT
doctl apps spec get "${APP_ID}" > "${PATCHED_SPEC}"

python - "${PATCHED_SPEC}" <<'PYEOF'
from pathlib import Path
import os
import re
import sys

spec_path = Path(sys.argv[1])
text = spec_path.read_text(encoding="utf-8")

env_values = {
    "LG_PROFILE": (os.environ["LG_PROFILE"], False),
    "LG_REMOTE_API_AUTH_MODE": ("bearer", False),
    "LG_REMOTE_API_TRUST_FORWARDED_HEADERS": ("true", False),
    "LG_REMOTE_API_BEARER_TOKEN": (os.environ["LG_REMOTE_API_BEARER_TOKEN"], True),
    "LG_RUNNER_API_KEY": (os.environ["LG_RUNNER_API_KEY"], True),
    "DIGITAL_OCEAN_MODEL_ACCESS_KEY": (os.environ["DIGITAL_OCEAN_MODEL_ACCESS_KEY"], True),
    "MODEL_ACCESS_KEY": (os.environ["MODEL_ACCESS_KEY"], True),
    "LG_PLANNER_PROVIDER": (os.environ["LG_PLANNER_PROVIDER"], False),
    "LG_PLANNER_MODEL": (os.environ["LG_PLANNER_MODEL"], False),
    "LG_ROUTER_PROVIDER": (os.environ["LG_ROUTER_PROVIDER"], False),
    "LG_ROUTER_MODEL": (os.environ["LG_ROUTER_MODEL"], False),
}
if os.environ.get("LG_CHECKPOINT_REDIS_URL"):
    env_values["LG_CHECKPOINT_REDIS_URL"] = (os.environ["LG_CHECKPOINT_REDIS_URL"], True)

services_split = text.split("    envs:\n", 1)
if len(services_split) != 2:
    raise SystemExit("envs section not found in app spec")

prefix, suffix = services_split
env_section, remainder = suffix, ""
service_tail_match = re.search(r"\n\s{4}[A-Za-z_].*", suffix)
if service_tail_match:
    env_section = suffix[: service_tail_match.start()]
    remainder = suffix[service_tail_match.start() :]

blocks: list[tuple[str, list[str]]] = []
current_key = None
current_block: list[str] = []
for raw_line in env_section.splitlines():
    if raw_line.startswith("      - key: "):
        if current_key is not None:
            blocks.append((current_key, current_block))
        current_key = raw_line.split(": ", 1)[1].strip()
        current_block = [raw_line]
    elif current_key is not None:
        current_block.append(raw_line)

if current_key is not None:
    blocks.append((current_key, current_block))

ordered_keys = [key for key, _ in blocks]
for key in env_values:
    if key not in ordered_keys:
        ordered_keys.append(key)

updated_blocks: dict[str, list[str]] = {}
for key in ordered_keys:
    if key in env_values:
        value, is_secret = env_values[key]
        block = [f"      - key: {key}"]
        if is_secret:
          block.extend([
              f"        value: {value}",
              "        scope: RUN_TIME",
              "        type: SECRET",
          ])
        else:
          block.append(f"        value: {value}")
        updated_blocks[key] = block
    else:
        for existing_key, existing_block in blocks:
            if existing_key == key:
                updated_blocks[key] = existing_block
                break

new_env_lines: list[str] = []
for key in ordered_keys:
    new_env_lines.extend(updated_blocks[key])

spec_path.write_text(prefix + "    envs:\n" + "\n".join(new_env_lines) + remainder, encoding="utf-8")
PYEOF

doctl apps update "${APP_ID}" --spec "${PATCHED_SPEC}"

if [[ "${DEPLOY_STACK}" == "split" ]]; then
  export DO_APP_ID="${APP_ID}"
  bash scripts/do_deploy_k8s.sh "${IMAGE_TAG}"
fi

LIVE_URL="$(doctl apps get "${APP_ID}" --format LiveURL --no-header 2>/dev/null || true)"

echo ""
echo "=== One-shot deployment complete ==="
echo "App ID:        ${APP_ID}"
echo "Image tag:     ${IMAGE_TAG}"
echo "Planner model: ${LG_PLANNER_MODEL}"
echo "Router model:  ${LG_ROUTER_MODEL}"
if [[ -n "${LIVE_URL}" ]]; then
  echo "Live URL:       ${LIVE_URL}"
  echo "UI URL:         ${LIVE_URL%/}/app/?access_token=${LG_REMOTE_API_BEARER_TOKEN}"
fi
