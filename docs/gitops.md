# GitOps Runbook — Lula Platform (Wave 10.8)

ArgoCD watches the `infra/k8s/` directory on the `main` branch and reconciles
cluster state automatically on every push.

---

## Prerequisites

| Tool | Minimum version | Notes |
|------|----------------|-------|
| `kubectl` | 1.28 | Configured for the target cluster |
| `argocd` CLI | 2.10 | `brew install argocd` / [releases](https://github.com/argoproj/argo-cd/releases) |
| Cluster access | — | `kubectl get nodes` must succeed |
| Git remote | — | The repository must be accessible from the cluster (public, or SSH/HTTPS credentials configured in ArgoCD) |

---

## Bootstrap (first time only)

Run once per cluster. The script is idempotent.

```bash
export REPO_URL=https://github.com/your-org/lula   # replace with actual remote
./scripts/argocd_bootstrap.sh
```

The script performs these steps in order:

1. Creates the `argocd` namespace (idempotent).
2. Installs ArgoCD stable from the upstream manifests.
3. Waits up to 120 s for `argocd-server` to become available.
4. Applies `infra/k8s/argocd-app.yaml` with `REPLACE_WITH_REPO_URL` substituted.
5. Applies `infra/k8s/argocd-rbac.yaml` (ClusterRole + ClusterRoleBinding).
6. Prints the command to retrieve the initial admin password.

Retrieve the admin password:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d && echo
```

Log in to the ArgoCD API server:

```bash
# Port-forward (local dev)
kubectl port-forward svc/argocd-server -n argocd 8080:443

# Log in
argocd login localhost:8080 --username admin --password <PASSWORD> --insecure
```

---

## Checking sync status

```bash
argocd app get lula
```

Key fields to inspect: `Sync Status`, `Health Status`, and any `OutOfSync` resources listed at the bottom.

View live diff between Git and cluster:

```bash
argocd app diff lula
```

---

## Manually triggering a sync

ArgoCD auto-syncs on push, but you can force an immediate sync:

```bash
argocd app sync lula
```

Force a hard refresh (re-reads Git, ignores cache):

```bash
argocd app sync lula --force
```

---

## Deploying image updates

1. Update the image tag in the relevant manifest:
   - Orchestrator: [`infra/k8s/deployment.yaml`](../infra/k8s/deployment.yaml) — change `image:` value.
   - Runner: [`infra/k8s/runner-deployment.yaml`](../infra/k8s/runner-deployment.yaml) — change `image:` value.
2. Commit and push to `main`.
3. ArgoCD detects the diff within the polling interval (default 3 min) and rolls out the new image automatically.

No manual `kubectl` commands are required.

---

## Rotating secrets

ArgoCD does **not** manage `Secret` objects directly (they are excluded from the
repository to avoid storing credentials in Git).

To rotate a secret:

```bash
# Edit the values
kubectl -n lula-orch edit secret lula-secrets

# Or recreate from the example file (fill in real values first):
cp infra/k8s/secrets.yaml.example /tmp/lula-secrets.yaml
# ... edit /tmp/lula-secrets.yaml ...
kubectl apply -f /tmp/lula-secrets.yaml
rm /tmp/lula-secrets.yaml
```

After the secret is updated the running pods will pick up the new values on
their next restart. To force an immediate rollout:

```bash
kubectl -n lula-orch rollout restart deployment/lula-orch deployment/lula-runner
```

---

## Rollback procedure

### Option A — revert in Git (preferred)

```bash
git revert <bad-commit-sha>
git push origin main
# ArgoCD auto-syncs to the reverted state within minutes.
```

### Option B — ArgoCD history rollback

```bash
# List available history
argocd app history lula

# Roll back to a specific revision
argocd app rollback lula <REVISION-ID>
```

Note: after a rollback ArgoCD disables auto-sync for the app until you re-enable it:

```bash
argocd app set lula --sync-policy automated
```

### Option C — emergency kubectl rollout undo

```bash
kubectl -n lula-orch rollout undo deployment/lula-orch
kubectl -n lula-orch rollout undo deployment/lula-runner
```

This bypasses ArgoCD and will cause a drift until the next Git push re-aligns
the cluster. Use only as a last resort.

---

## Relevant manifests

| File | Purpose |
|------|---------|
| [`infra/k8s/argocd-app.yaml`](../infra/k8s/argocd-app.yaml) | ArgoCD Application CRD |
| [`infra/k8s/argocd-rbac.yaml`](../infra/k8s/argocd-rbac.yaml) | ClusterRole + ClusterRoleBinding for the controller |
| [`scripts/argocd_bootstrap.sh`](../scripts/argocd_bootstrap.sh) | One-shot bootstrap script |
| [`infra/k8s/deployment.yaml`](../infra/k8s/deployment.yaml) | Orchestrator Deployment |
| [`infra/k8s/runner-deployment.yaml`](../infra/k8s/runner-deployment.yaml) | Runner Deployment |
| [`infra/k8s/hpa.yaml`](../infra/k8s/hpa.yaml) | HorizontalPodAutoscalers |
| [`infra/k8s/pdb.yaml`](../infra/k8s/pdb.yaml) | PodDisruptionBudgets (`lula-orch-pdb`, `lula-runner-pdb`) — ensures `minAvailable: 1` pod during voluntary disruptions (node drain, upgrades) |
| [`infra/k8s/namespace.yaml`](../infra/k8s/namespace.yaml) | `lula-orch` Namespace |
