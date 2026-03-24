#!/usr/bin/env bash
# do_deploy_k8s.sh — Build, push, and deploy the Rust runner to a DOKS cluster
# with gVisor sandboxing.  The Python API is deployed separately on App Platform.
#
# Architecture:
#   - lula-runner (Deployment)  — Rust binary only, gVisor RuntimeClass, LoadBalancer Service
#   - Python API                — App Platform (infra/do/app.yaml), pointed at runner LB IP
#     via LG_RUNNER_BASE_URL env var, OR the combined image on a Droplet.
#
# Usage:
#   Copy infra/k8s/secrets.yaml.example -> infra/k8s/secrets.yaml and fill in values.
#   DO_REGISTRY=lula-orch bash scripts/do_deploy_k8s.sh [IMAGE_TAG]
set -euo pipefail

usage() {
  echo "Usage: DO_REGISTRY=<registry-name> bash scripts/do_deploy_k8s.sh [IMAGE_TAG]"
  echo ""
  echo "Required environment variables:"
  echo "  DO_REGISTRY                    DOCR registry name (e.g. lula-orch)"
  echo ""
  echo "Optional environment variables:"
  echo "  DO_CLUSTER_NAME                DOKS cluster name (default: lula-orch)"
  echo "  DO_REGION                      DigitalOcean region slug (default: nyc3)"
  echo "  DO_K8S_VERSION                 Kubernetes version (default: latest)"
  echo "  DO_APP_ID                      App Platform app ID (to auto-update LG_RUNNER_BASE_URL)"
  echo "  LG_CHECKPOINT_REDIS_URL        Valkey/Redis checkpoint URI (set this in App Platform)"
  echo ""
  echo "Optional positional argument:"
  echo "  IMAGE_TAG                      Docker image tag (default: latest)"
  exit 1
}

# --- Required ---
DO_REGISTRY="${DO_REGISTRY:-}"
if [[ -z "${DO_REGISTRY}" ]]; then
  echo "ERROR: DO_REGISTRY is required." >&2
  usage
fi

# --- Optional with defaults ---
DO_CLUSTER_NAME="${DO_CLUSTER_NAME:-lula-orch}"
DO_REGION="${DO_REGION:-nyc3}"
DO_K8S_VERSION="${DO_K8S_VERSION:-}"
DO_APP_ID="${DO_APP_ID:-}"

# --- Positional ---
IMAGE_TAG="${1:-latest}"

# --- Derived ---
IMAGE="registry.digitalocean.com/${DO_REGISTRY}/lula-orch:${IMAGE_TAG}"

SECRETS_FILE="infra/k8s/secrets.yaml"
if [[ ! -f "${SECRETS_FILE}" ]]; then
  echo "ERROR: ${SECRETS_FILE} not found." >&2
  echo "  Copy infra/k8s/secrets.yaml.example to infra/k8s/secrets.yaml and fill in real values." >&2
  exit 1
fi

TOTAL_STEPS=14

echo "--- [step 1/${TOTAL_STEPS}] Ensure DOCR registry exists ---"
doctl registry get "${DO_REGISTRY}" 2>/dev/null \
  || doctl registry create "${DO_REGISTRY}" --region "${DO_REGION}"

echo "--- [step 2/${TOTAL_STEPS}] Login to DOCR ---"
doctl registry login

echo "--- [step 3/${TOTAL_STEPS}] Build Docker image (linux/amd64) ---"
docker build --platform linux/amd64 -t "${IMAGE}" .

echo "--- [step 4/${TOTAL_STEPS}] Push Docker image to DOCR ---"
docker push "${IMAGE}"

echo "--- [step 5/${TOTAL_STEPS}] Create DOKS cluster if absent ---"
if doctl kubernetes cluster get "${DO_CLUSTER_NAME}" 2>/dev/null; then
  echo "Cluster '${DO_CLUSTER_NAME}' already exists — skipping creation."
else
  CREATE_ARGS=(
    kubernetes cluster create "${DO_CLUSTER_NAME}"
    --region "${DO_REGION}"
    --count 2
    --size s-2vcpu-4gb
    --node-pool "name=gvisor-pool;count=2;size=s-2vcpu-4gb;label=sandbox=gvisor;taint=sandbox=gvisor:NoSchedule"
  )
  if [[ -n "${DO_K8S_VERSION}" ]]; then
    CREATE_ARGS+=(--version "${DO_K8S_VERSION}")
  fi
  doctl "${CREATE_ARGS[@]}"
fi

echo "--- [step 6/${TOTAL_STEPS}] Save kubeconfig ---"
doctl kubernetes cluster kubeconfig save "${DO_CLUSTER_NAME}"

echo "--- [step 7/${TOTAL_STEPS}] Apply namespace ---"
kubectl apply -f infra/k8s/namespace.yaml

echo "--- [step 8/${TOTAL_STEPS}] Create DOCR pull secret ---"
doctl registry kubernetes-manifest --namespace lula-orch | kubectl apply -f - || true
kubectl create secret docker-registry docr-secret \
  --namespace lula-orch \
  --docker-server="registry.digitalocean.com" \
  --docker-username="$(doctl auth whoami)" \
  --docker-password="$(doctl auth token)" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "--- [step 9/${TOTAL_STEPS}] Apply gVisor RuntimeClass ---"
kubectl apply -f infra/k8s/gvisor-runtime-class.yaml

echo "--- [step 10/${TOTAL_STEPS}] Apply gVisor installer DaemonSet ---"
kubectl apply -f infra/k8s/gvisor-installer.yaml
echo "Waiting 20s for gVisor installer to run on nodes..."
sleep 20

echo "--- [step 11/${TOTAL_STEPS}] Apply secrets ---"
kubectl apply -f "${SECRETS_FILE}"

echo "--- [step 12/${TOTAL_STEPS}] Apply runner Deployment and LoadBalancer Service ---"
# Patch image tag in runner-deployment before applying
PATCHED_RUNNER_DEPLOY="$(mktemp --suffix=.yaml)"
trap "rm -f '${PATCHED_RUNNER_DEPLOY}'" EXIT
sed "s|lula-orch:latest|lula-orch:${IMAGE_TAG}|g" infra/k8s/runner-deployment.yaml > "${PATCHED_RUNNER_DEPLOY}"
kubectl apply -f "${PATCHED_RUNNER_DEPLOY}"
kubectl apply -f infra/k8s/runner-service.yaml

echo "--- [step 13/${TOTAL_STEPS}] Wait for runner rollout ---"
kubectl rollout status deployment/lula-runner -n lula-orch --timeout=300s

echo "--- [step 14/${TOTAL_STEPS}] Retrieve runner LoadBalancer IP ---"
RUNNER_LB_IP=""
for _ in $(seq 1 30); do
  RUNNER_LB_IP="$(kubectl get svc lula-runner -n lula-orch \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
  if [[ -n "${RUNNER_LB_IP}" && "${RUNNER_LB_IP}" != "null" ]]; then
    break
  fi
  echo "  waiting for LoadBalancer IP..."
  sleep 10
done

RUNNER_URL="http://${RUNNER_LB_IP}:8088"

echo ""
echo "=== Deployment complete ==="
echo "Runner LoadBalancer IP : ${RUNNER_LB_IP}"
echo "Runner URL             : ${RUNNER_URL}"
echo ""

if [[ -n "${DO_APP_ID}" ]]; then
  echo "Updating App Platform LG_RUNNER_BASE_URL -> ${RUNNER_URL} ..."
  PATCHED_APP_SPEC="$(mktemp --suffix=.yaml)"
  trap "rm -f '${PATCHED_RUNNER_DEPLOY}' '${PATCHED_APP_SPEC}'" EXIT
  # Inject LG_RUNNER_BASE_URL env var into app spec (append to envs block)
  python3 - "${RUNNER_URL}" infra/do/app.yaml "${PATCHED_APP_SPEC}" <<'PYEOF'
import sys, re
runner_url, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(src).read()
# Remove any existing LG_RUNNER_BASE_URL entry
text = re.sub(
    r'\s*- key: LG_RUNNER_BASE_URL\n(?:    .*\n)*',
    '\n',
    text,
)
# Append before last line of envs block (after last env entry before next section)
inject = f"\n      - key: LG_RUNNER_BASE_URL\n        value: {runner_url}\n"
# Insert after last env entry
text = re.sub(r'(\n      - key: LG_ROUTER_MODEL\n        value: .*)', r'\1' + inject, text)
open(dst, 'w').write(text)
PYEOF
  doctl apps update "${DO_APP_ID}" --spec "${PATCHED_APP_SPEC}"
  echo "App Platform updated. Runner URL set to: ${RUNNER_URL}"
  echo "Make sure App Platform secret LG_CHECKPOINT_REDIS_URL is set to your DO Managed Valkey URI."
else
  echo "NEXT STEPS:"
  echo "  Set LG_RUNNER_BASE_URL=${RUNNER_URL} in the App Platform environment:"
  echo "    doctl apps update <APP_ID> --spec infra/do/app.yaml"
  echo "  Set LG_CHECKPOINT_REDIS_URL=<your-valkey-uri> in the same App Platform environment."
  echo "  Or via DO console > App Settings > Environment Variables."
fi
