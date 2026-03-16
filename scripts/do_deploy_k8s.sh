#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: DO_REGISTRY=<registry-name> bash scripts/do_deploy_k8s.sh [IMAGE_TAG]"
  echo ""
  echo "Required environment variables:"
  echo "  DO_REGISTRY                    DOCR registry name (e.g. lula-orch)"
  echo ""
  echo "Optional environment variables:"
  echo "  DO_CLUSTER_NAME                DOKS cluster name (default: lula-orch)"
  echo "  DO_REGION                      DigitalOcean region (default: nyc3)"
  echo "  DO_K8S_VERSION                 Kubernetes version (default: latest)"
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

# --- Positional ---
IMAGE_TAG="${1:-latest}"

# --- Derived ---
IMAGE="registry.digitalocean.com/${DO_REGISTRY}/lula-orch:${IMAGE_TAG}"

TOTAL_STEPS=13

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
doctl registry kubernetes-manifest --namespace lula-orch | kubectl apply -f -
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

echo "--- [step 11/${TOTAL_STEPS}] Apply secrets ---"
kubectl apply -f infra/k8s/secrets.yaml

echo "--- [step 12/${TOTAL_STEPS}] Apply deployment, service, and ingress ---"
kubectl apply -f infra/k8s/deployment.yaml
kubectl apply -f infra/k8s/service.yaml
kubectl apply -f infra/k8s/ingress.yaml

echo "--- [step 13/${TOTAL_STEPS}] Wait for rollout ---"
kubectl rollout status deployment/lula-orch -n lula-orch --timeout=180s

echo ""
echo "=== Deployment complete ==="
echo "Ingress host : lula-orch.example.com  (update infra/k8s/ingress.yaml with real domain)"
LB_IP=$(kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "<pending>")
echo "LoadBalancer IP : ${LB_IP}"
echo ""
echo "NEXT STEPS:"
echo "  1. Point your domain DNS A record to ${LB_IP}"
echo "  2. Edit infra/k8s/secrets.yaml — replace REPLACE_ME values — then: kubectl apply -f infra/k8s/secrets.yaml"
echo "  3. Edit infra/k8s/ingress.yaml — replace lula-orch.example.com — then: kubectl apply -f infra/k8s/ingress.yaml"
echo "  4. Restart rollout after secrets update: kubectl rollout restart deployment/lula-orch -n lula-orch"
