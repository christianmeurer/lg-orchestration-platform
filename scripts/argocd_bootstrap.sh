#!/usr/bin/env bash
# Usage: REPO_URL=https://github.com/your-org/lula ./scripts/argocd_bootstrap.sh
#
# Installs ArgoCD (stable) into the cluster, waits for CRDs and the server
# deployment to become available, then applies the Lula Application manifest
# and RBAC bindings.
#
# Prerequisites:
#   - kubectl configured and pointing at the target cluster
#   - REPO_URL environment variable set to the Git remote for this repository
#
# Example:
#   REPO_URL=https://github.com/acme/lula ./scripts/argocd_bootstrap.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ARGOCD_APP_MANIFEST="${REPO_ROOT}/infra/k8s/argocd-app.yaml"
ARGOCD_RBAC_MANIFEST="${REPO_ROOT}/infra/k8s/argocd-rbac.yaml"
ARGOCD_PROJECT_MANIFEST="${REPO_ROOT}/infra/k8s/argocd-project.yaml"
ARGOCD_STABLE_INSTALL="https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml"
ARGOCD_IMAGE_UPDATER_INSTALL="https://raw.githubusercontent.com/argoproj-labs/argocd-image-updater/stable/manifests/install.yaml"

if [[ -z "${REPO_URL:-}" ]]; then
  echo "ERROR: REPO_URL is not set." >&2
  echo "  Usage: REPO_URL=https://github.com/your-org/lula ${0}" >&2
  exit 1
fi

echo "==> [1/8] Creating argocd namespace (idempotent)"
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -

echo "==> [2/8] Installing ArgoCD (stable)"
kubectl apply -n argocd -f "${ARGOCD_STABLE_INSTALL}"

echo "==> [3/8] Waiting for argocd-server deployment to become available (timeout: 120s)"
kubectl wait \
  --for=condition=available \
  --timeout=120s \
  deployment/argocd-server \
  -n argocd

echo "==> [4/8] Installing ArgoCD Image Updater"
kubectl apply -n argocd -f "${ARGOCD_IMAGE_UPDATER_INSTALL}"
kubectl rollout status -n argocd deployment/argocd-image-updater --timeout=120s

echo "==> [5/8] Applying Lula AppProject (must precede the Application)"
kubectl apply -f "${ARGOCD_PROJECT_MANIFEST}"

echo "==> [6/8] Applying Lula ArgoCD Application (REPO_URL=${REPO_URL})"
# Substitute the placeholder in argocd-app.yaml and pipe directly to kubectl.
# The source file is never modified on disk.
sed "s|REPLACE_WITH_REPO_URL|${REPO_URL}|g" "${ARGOCD_APP_MANIFEST}" \
  | kubectl apply -f -

echo "==> [7/8] Applying ArgoCD RBAC (ClusterRole + ClusterRoleBinding)"
kubectl apply -f "${ARGOCD_RBAC_MANIFEST}"

echo "==> [8/8] Bootstrap complete."
echo ""
echo "Retrieve the initial ArgoCD admin password with:"
echo "  kubectl -n argocd get secret argocd-initial-admin-secret \\"
echo "    -o jsonpath='{.data.password}' | base64 -d && echo"
echo ""
echo "Check sync status with:"
echo "  argocd app get lula"
