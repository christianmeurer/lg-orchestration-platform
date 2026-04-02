#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${1:-lula-orch}"

echo "=== Lula Deployment Verification ==="

# Check pods
echo -n "Pods: "
RUNNING=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | grep -c Running || true)
echo "$RUNNING running"

# Health check
echo -n "Health: "
ORCH_POD=$(kubectl get pods -n "$NAMESPACE" -l app=lula-orch -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [ -n "$ORCH_POD" ]; then
  kubectl exec -n "$NAMESPACE" "$ORCH_POD" -- curl -sf http://localhost:8001/healthz 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK' if d.get('ok') else 'FAIL')" 2>/dev/null \
    || echo "FAIL"
else
  echo "FAIL (no orchestrator pod found)"
fi

# Runner health
echo -n "Runner: "
RUNNER_POD=$(kubectl get pods -n "$NAMESPACE" -l app=lula-runner -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [ -n "$RUNNER_POD" ]; then
  kubectl exec -n "$NAMESPACE" "$RUNNER_POD" -- curl -sf http://localhost:8088/healthz 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK' if d.get('ok') else 'FAIL')" 2>/dev/null \
    || echo "FAIL"
else
  echo "FAIL (no runner pod found)"
fi

# API endpoint
echo -n "API: "
if [ -n "$ORCH_POD" ] && [ -n "${LULA_TOKEN:-}" ]; then
  kubectl exec -n "$NAMESPACE" "$ORCH_POD" -- curl -sf http://localhost:8001/v1/runs -H "Authorization: Bearer $LULA_TOKEN" > /dev/null 2>&1 \
    && echo "OK" || echo "FAIL"
else
  echo "skipped (no LULA_TOKEN or no pod)"
fi

# Certificate status (if cert-manager installed)
echo -n "TLS: "
kubectl get certificate -n "$NAMESPACE" 2>/dev/null | grep -q True && echo "OK" || echo "pending"

# Ingress
echo -n "Ingress: "
INGRESS=$(kubectl get ingress -n "$NAMESPACE" --no-headers 2>/dev/null | head -1 | awk '{print $4}')
if [ -n "$INGRESS" ]; then
  echo "$INGRESS"
else
  echo "none"
fi

echo "=== Verification complete ==="
