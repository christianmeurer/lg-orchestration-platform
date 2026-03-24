# Lula — Production Deployment Plan

**Target platform:** DigitalOcean  
**Audience:** Developer / DevOps engineer deploying from scratch  
**Last updated:** 2026-03-20

---

## Table of Contents

1. [Architecture Summary](#1-architecture-summary)
2. [Deployment Tiers](#2-deployment-tiers)
3. [Detailed Hardware Specifications](#3-detailed-hardware-specifications)
4. [Network Architecture](#4-network-architecture)
5. [Step-by-Step Deployment — Tier 2 (Primary)](#5-step-by-step-deployment--tier-2-primary)
6. [Secrets Reference](#6-secrets-reference)
7. [Firecracker Setup (Tier 3 Addendum)](#7-firecracker-setup-tier-3-addendum)
8. [Monitoring and Observability](#8-monitoring-and-observability)
9. [Scaling Guidelines](#9-scaling-guidelines)
10. [Disaster Recovery](#10-disaster-recovery)
11. [Cost Optimization](#11-cost-optimization)

---

## 1. Architecture Summary

Lula is a two-component system. The Python LangGraph orchestrator (`lula-orch`) drives the full plan/execute/verify/recover loop. All tool calls are dispatched over HTTP to the Rust runner (`lula-runner`), which enforces path confinement, command allowlists, sandbox isolation, and HMAC-signed approval gates before touching the filesystem or spawning subprocesses.

### Component Responsibilities

| Component | Binary | Language | Role |
|---|---|---|---|
| `lula-orch` | `lg_orch` (Python/uvicorn) | Python 3.12 + LangGraph | Reasoning, routing, checkpointing, API surface |
| `lula-runner` | `lg-runner` (Rust) | Rust 1.88 + Tokio | Tool execution, sandbox isolation, approval gates |

### Communication Topology

- **External traffic:** HTTPS → Ingress Controller → `lula-orch` service (port 8001). The orchestrator is the only publicly reachable component.
- **Internal traffic:** `lula-orch` pods → `lula-runner` service (HTTP, port 8088). Enforced by [`infra/k8s/network-policy.yaml`](../infra/k8s/network-policy.yaml).
- **No external traffic from runner:** The `lula-runner` pods have no egress to the internet. DNS resolution is permitted only to `kube-system`. A back-channel on port 8765 allows the runner to emit approval events back to the orchestrator.
- **Firecracker assets:** `rootfs.ext4` and `vmlinux` must be present on the Kubernetes node at `/opt/lula/`. They are not bundled in the container image. They are mounted via a `hostPath` volume (see [`infra/k8s/runner-deployment.yaml`](../infra/k8s/runner-deployment.yaml)).

### Sandbox Stack (Tiered Degradation)

The runner selects the highest available isolation tier at startup:

```
Firecracker MicroVM  (full VM isolation via vsock AF_VSOCK CID 3:52525, Linux only)
    ↓ fallback
Linux Namespaces     (user/PID/net/mount unshare, Linux only)
    ↓ fallback
SafeFallback         (env_clear + command allowlist + path confinement, all platforms)
```

gVisor (`runtimeClassName: gvisor`) provides an additional kernel interposition layer when deployed on Kubernetes, regardless of the in-process sandbox tier.

### Container Image

A single multi-stage Docker image ([`Dockerfile`](../Dockerfile)) contains both binaries:

- **Stage 1** (rust-builder): Compiles `lg-runner` from `rs/`
- **Stage 2** (python-builder): Installs Python deps via `uv`
- **Stage 3** (runtime): Debian Bookworm Slim, copies both artifacts

The image is published to `registry.digitalocean.com/lula-registry/lula` on every `v*.*.*` tag push via [`.github/workflows/release.yml`](../.github/workflows/release.yml). The release pipeline pins the image digest in [`infra/k8s/deployment.yaml`](../infra/k8s/deployment.yaml) automatically.

---

## 2. Deployment Tiers

### Tier 1 — Single Droplet or App Platform (Development / Demo)

**Use case:** Personal use, feature demos, CI smoke-testing, evaluation runs  
**Isolation:** SafeFallback only (no gVisor, no Firecracker)  
**HA:** None  

The combined image (`Dockerfile`) launches both the orchestrator and runner as a single process via `scripts/start_remote_stack.sh`. The runner listens on `127.0.0.1:8088`; the orchestrator API on `0.0.0.0:8001`.

Two sub-options:

| Sub-option | Spec | Cost/mo | Notes |
|---|---|---|---|
| DO App Platform Basic | `apps-s-1vcpu-1gb` | ~$5 | Managed TLS, zero-downtime redeploy, no SSH |
| DigitalOcean Droplet | `s-4vcpu-8gb` | ~$48 | Full SSH, manual nginx+certbot TLS |

Deploy with:

```bash
# App Platform
export DO_REGISTRY=lula-registry
export LG_REMOTE_API_BEARER_TOKEN=$(openssl rand -hex 32)
export LG_RUNNER_API_KEY=$(openssl rand -hex 32)
export DIGITAL_OCEAN_MODEL_ACCESS_KEY=<your-do-genai-key>
doctl registry create lula-registry --region nyc3
doctl registry login
docker build --platform linux/amd64 \
  -t registry.digitalocean.com/lula-registry/lula:latest .
docker push registry.digitalocean.com/lula-registry/lula:latest
doctl apps create --spec infra/do/app.yaml
```

Config profile: `LG_PROFILE=prod` — uses [`configs/runtime.prod.toml`](../configs/runtime.prod.toml) with `SafeFallback` as the active sandbox tier when KVM is unavailable.

---

### Tier 2 — Managed Kubernetes (Production, gVisor Isolation)

**Use case:** Production workloads, multi-tenant, regulated environments  
**Isolation:** gVisor (`runtimeClassName: gvisor`) on all pods  
**HA:** Yes — PodDisruptionBudgets, HPA, multi-node pools  
**GitOps:** ArgoCD with ArgoCD Image Updater (semver tracking)

DOKS cluster with two node pools:

| Pool | Purpose | Label / Taint |
|---|---|---|
| `default-pool` | Orchestrator workloads, ingress | none |
| `gvisor-pool` | Runner + orchestrator pods | `sandbox=gvisor` label + `NoSchedule` taint |

All pods in [`infra/k8s/deployment.yaml`](../infra/k8s/deployment.yaml) and [`infra/k8s/runner-deployment.yaml`](../infra/k8s/runner-deployment.yaml) carry `runtimeClassName: gvisor` and are scheduled onto gVisor nodes.

Persistent services:
- **DO Managed Valkey** — LangGraph checkpoint backend (keep [`backend = "redis"`](../configs/runtime.prod.toml:59) and [`LG_CHECKPOINT_REDIS_URL`](../README.md:151); Valkey is used as the managed Redis-compatible service)
- **DO Managed PostgreSQL** — Optional audit trail backend (can use S3 JSONL instead)
- **DO Load Balancer** — Provisioned automatically by NGINX Ingress Controller

---

### Tier 3 — Full Production (Firecracker MicroVM)

**Use case:** Maximum isolation, multi-tenant code execution, compliance-gated deployments  
**Isolation:** Firecracker MicroVM (full VM per tool batch) + gVisor container runtime  
**Additional requirements:** KVM-capable nodes, pre-baked `/opt/lula/rootfs.ext4` and `/opt/lula/vmlinux`

Requires dedicated Firecracker-capable nodes (large Droplets with nested virtualization or bare metal). The runner reads `LG_RUNNER_ROOTFS_IMAGE` and `LG_RUNNER_KERNEL_IMAGE` from environment (defaulting to `/opt/lula/rootfs.ext4` and `/opt/lula/vmlinux`). These are mounted from the node at `/opt/lula/` via the `firecracker-assets` hostPath volume in [`infra/k8s/runner-deployment.yaml`](../infra/k8s/runner-deployment.yaml).

See [Section 7](#7-firecracker-setup-tier-3-addendum) for full Firecracker node setup procedure.

---

## 3. Detailed Hardware Specifications

### Tier 1 — App Platform / Single Droplet

| Component | Spec | Cost/mo |
|---|---|---|
| Combined image (orch + runner) | `apps-s-1vcpu-1gb` or `s-4vcpu-8gb` Droplet | $5–$48 |
| Valkey | SQLite file checkpoint (dev) or DO Managed Valkey 1-node | $0–$15 |
| PostgreSQL | None (SQLite run store) | $0 |
| **Total** | | **~$5–$63** |

### Tier 2 — DOKS Production (Recommended)

| Component | Min | Recommended | Max (autoscale) | Cost/mo |
|---|---|---|---|---|
| Orchestrator (`lula-orch`) | 1× `s-2vcpu-4gb` | 2× `s-2vcpu-4gb` | 10 pods via HPA | $24–$240 |
| Runner (`lula-runner`) | 1× `s-4vcpu-8gb` | 2× `s-4vcpu-8gb` | 20 pods via HPA | $48–$960 |
| Valkey (checkpoint) | `db-s-1vcpu-1gb` | `db-s-1vcpu-1gb` | Single node (Redis-compatible) | $15 |
| PostgreSQL (audit) | `db-s-1vcpu-1gb` | `db-s-1vcpu-1gb` | Optional (S3 cheaper) | $15 |
| DO Load Balancer | 1 LB | 1 LB | — | $12 |
| DOCR (container registry) | Starter (500 MB) | Basic (5 GB) | — | $0–$5 |
| **Total (baseline 2+2 nodes)** | **~$114** | **~$200–$250** | **~$1,257 (full scale)** | |

Baseline node cost breakdown:
- 2× orchestrator nodes `s-2vcpu-4gb` = 2 × $24 = $48/mo
- 2× runner nodes `s-4vcpu-8gb` = 2 × $48 = $96/mo
- Valkey + PostgreSQL + LB = $42/mo
- **Baseline: ~$186/mo** (before autoscale)

Pod resource requests (from manifests):

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|---|---|---|---|---|
| `lula-orch` | 500m | 2000m | 512Mi | 2Gi |
| `lula-runner` | 500m | 2000m | 512Mi | 2Gi |

### Tier 3 — Full Production with Firecracker

Add to Tier 2:

| Component | Spec | Cost/mo | Notes |
|---|---|---|---|
| Firecracker nodes | 2× `s-8vcpu-16gb` | ~$192 | Must support KVM (`/dev/kvm`) |
| Additional runner pool | Dedicated `firecracker-pool` | $192+ | Tainted `sandbox=firecracker:NoSchedule` |
| **Tier 3 total** | | **~$430–$600** | |

---

## 4. Network Architecture

### Traffic Flow Diagram

```
Internet
    │
    ▼ HTTPS :443
DO Load Balancer (provisioned by NGINX Ingress)
    │
    ▼ HTTP :80 (redirects to 443) / :443
NGINX Ingress Controller (cert-manager TLS termination)
    │  cert-manager issues Let's Encrypt cert for domain
    ▼ HTTP :8001
lula-orch Service (ClusterIP)
    │
    ▼ HTTP :8001
lula-orch Pods (up to 10 via HPA)
    │  [NetworkPolicy allows egress to runner:8088]
    ▼ HTTP :8088
lula-runner Service (ClusterIP — internal only)
    │
    ▼ HTTP :8088
lula-runner Pods (up to 20 via HPA)
    │
    ├──▶ [gVisor syscall interposition]
    ├──▶ Firecracker MicroVM (if enabled) via AF_VSOCK
    └──▶ /opt/lula/rootfs.ext4, /opt/lula/vmlinux (HostPath volume)

lula-orch Pods
    │  [NetworkPolicy allows egress]
    ├──▶ Valkey :6379 (DO Managed Valkey — checkpoint store)
    ├──▶ PostgreSQL :5432 (DO Managed PG — audit trail, optional)
    ├──▶ inference.do-ai.run :443 (DO GenAI API)
    ├──▶ api.openai.com :443 (OpenAI fallback)
    └──▶ kube-system DNS :53

lula-runner Pods
    │  [NetworkPolicy DENIES all external egress]
    ├──▶ kube-system DNS :53 (UDP/TCP — only permitted egress)
    └──▶ lula-orch :8765 (approval event back-channel)
```

### Kubernetes Services

| Service | Type | Port | Selects |
|---|---|---|---|
| `lula-orch` | ClusterIP | 8001 | `app: lula-orch` |
| `lula-runner` | ClusterIP | 8088 | `app: lula-runner` |
| `ingress-nginx-controller` | LoadBalancer | 80, 443 | Nginx controller pods |

### NetworkPolicy Rules (from [`infra/k8s/network-policy.yaml`](../infra/k8s/network-policy.yaml))

**`lula-runner` NetworkPolicy:**

| Direction | Allowed | Port |
|---|---|---|
| Ingress | From `app: lula-orch` pods only | TCP 8088 |
| Egress | To `kube-system` namespace (DNS) | UDP/TCP 53 |
| Egress | To `app: lula-orch` pods (approval back-channel) | TCP 8765 |
| All other egress | DENIED | — |

The orchestrator has no restrictive NetworkPolicy applied — it uses the cluster default (allow all). For hardened deployments, apply an explicit NetworkPolicy permitting egress to Valkey/Redis-compatible checkpoint storage, PostgreSQL, the inference APIs, DNS, and the runner only.

### TLS

- Ingress controller: `kubernetes.io/ingress.class: nginx`
- TLS cert: `cert-manager.io/cluster-issuer: letsencrypt-prod` (see [`infra/k8s/ingress.yaml`](../infra/k8s/ingress.yaml))
- Proxy timeout: 3600 s (`nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"`) — required for long-running agent SSE streams
- Domain: replace `lula.eiv.eng.br` in [`infra/k8s/ingress.yaml`](../infra/k8s/ingress.yaml) with your domain before applying

---

## 5. Step-by-Step Deployment — Tier 2 (Primary)

### Step 1 — Install Prerequisites

```bash
# doctl (DigitalOcean CLI) >= 1.100
brew install doctl                    # macOS
# or: https://github.com/digitalocean/doctl/releases

# kubectl >= 1.28
brew install kubectl

# helm >= 3.14
brew install helm

# ArgoCD CLI >= 2.10
brew install argocd
# or: https://github.com/argoproj/argo-cd/releases

# uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Rust 1.88+
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
rustup update stable

# Docker >= 24 with BuildKit
# https://docs.docker.com/engine/install/

# Authenticate doctl
doctl auth init
```

Verify:

```bash
doctl account get
kubectl version --client
helm version
argocd version --client
```

### Step 2 — Create DOKS Cluster

```bash
export DO_REGION=nyc3
export DO_CLUSTER_NAME=lula-prod

doctl kubernetes cluster create "${DO_CLUSTER_NAME}" \
  --region "${DO_REGION}" \
  --version latest \
  --count 2 \
  --size s-2vcpu-4gb \
  --node-pool "name=gvisor-pool;count=2;size=s-4vcpu-8gb;label=sandbox=gvisor;taint=sandbox=gvisor:NoSchedule" \
  --wait
```

This creates:
- **Default pool** — 2× `s-2vcpu-4gb` for ingress and general workloads
- **`gvisor-pool`** — 2× `s-4vcpu-8gb` labeled `sandbox=gvisor`, tainted `NoSchedule` so only explicitly tolerated pods land here

Save kubeconfig:

```bash
doctl kubernetes cluster kubeconfig save "${DO_CLUSTER_NAME}"
kubectl get nodes -o wide   # verify all 4 nodes Ready
```

### Step 3 — Create Container Registry

```bash
export DO_REGISTRY=lula-registry

doctl registry create "${DO_REGISTRY}" --region "${DO_REGION}"
doctl registry login
```

The registry URL will be `registry.digitalocean.com/lula-registry`. Manifests in `infra/k8s/` reference `registry.digitalocean.com/lula-registry/lula`.

Add registry pull access to the cluster:

```bash
doctl registry kubernetes-manifest --namespace lula-orch \
  | kubectl apply -f -
```

### Step 4 — Build and Push the Container Image

```bash
export IMAGE_TAG=v1.0.0

docker build --platform linux/amd64 \
  -t "registry.digitalocean.com/${DO_REGISTRY}/lula:${IMAGE_TAG}" \
  -t "registry.digitalocean.com/${DO_REGISTRY}/lula:latest" \
  .

docker push "registry.digitalocean.com/${DO_REGISTRY}/lula:${IMAGE_TAG}"
docker push "registry.digitalocean.com/${DO_REGISTRY}/lula:latest"
```

Alternatively, push a `v*.*.*` Git tag and let the CI pipeline build, scan, and push automatically (see [`.github/workflows/release.yml`](../.github/workflows/release.yml)).

### Step 5 — Install gVisor on the Runner Node Pool

Apply the gVisor installer DaemonSet. It runs a privileged init container on every `sandbox=gvisor` node that:
1. Downloads `runsc` from the official gVisor GCS bucket
2. Installs it to `/usr/local/sbin/runsc`
3. Patches `/etc/containerd/config.toml` to register the `runsc` runtime handler
4. Leaves containerd restart as an out-of-band node operation after init completes

```bash
kubectl apply -f infra/k8s/gvisor-installer.yaml

# Wait for the DaemonSet to complete on all gVisor nodes
kubectl rollout status daemonset/gvisor-installer -n kube-system --timeout=120s

# Register the RuntimeClass
kubectl apply -f infra/k8s/gvisor-runtime-class.yaml

# Verify gVisor is installed and RuntimeClass exists
kubectl get runtimeclass gvisor
```

Verify the installer ran on each node:

```bash
kubectl get pods -n kube-system -l app=gvisor-installer -o wide
```

### Step 6 — Install Ingress Controller and cert-manager

```bash
# NGINX Ingress Controller (provisions a DO Load Balancer automatically)
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/cloud/deploy.yaml

# cert-manager
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml

# Wait for cert-manager to be ready
kubectl rollout status deployment/cert-manager -n cert-manager --timeout=120s

# Create Let's Encrypt ClusterIssuer
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ops@your-domain.com     # REPLACE with real email
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            class: nginx
EOF
```

### Step 7 — Create Kubernetes Namespace and Secrets

```bash
kubectl apply -f infra/k8s/namespace.yaml

# Copy the secrets template
cp infra/k8s/secrets.yaml.example /tmp/lula-secrets.yaml
```

Edit `/tmp/lula-secrets.yaml` and replace all `REPLACE_ME` values (see [Section 6](#6-secrets-reference) for details):

```yaml
stringData:
  LG_REMOTE_API_BEARER_TOKEN: "<openssl rand -hex 32>"
  LG_RUNNER_API_KEY:          "<openssl rand -hex 32>"
  DIGITAL_OCEAN_MODEL_ACCESS_KEY: "<your-do-genai-key>"
  MODEL_ACCESS_KEY:           "<your-openai-or-anthropic-key>"
```

Apply:

```bash
kubectl apply -f /tmp/lula-secrets.yaml
rm /tmp/lula-secrets.yaml   # never commit this file

# Create the DOCR pull secret
kubectl create secret docker-registry docr-secret \
  --namespace lula-orch \
  --docker-server="registry.digitalocean.com" \
  --docker-username="$(doctl auth whoami)" \
  --docker-password="$(doctl auth token)" \
  --dry-run=client -o yaml | kubectl apply -f -
```

### Step 8 — Provision Managed Redis

```bash
# Create DO Managed Valkey (1 vCPU / 1 GB, NYC3)
doctl databases create lula-valkey \
  --engine valkey \
  --region nyc3 \
  --size db-s-1vcpu-1gb \
  --num-nodes 1 \
  --version 8

# Look up the cluster ID (doctl get/connection require the database ID, not the name)
DB_ID="$(doctl databases list --format ID,Name --no-header | awk '$2=="lula-valkey" {print $1}')"

# Wait until status = online
doctl databases get "${DB_ID}" --format Name,Status

# Get the connection string
doctl databases connection "${DB_ID}" --format URI
```

Add the Valkey URI as an additional secret. Keep the existing environment variable name [`LG_CHECKPOINT_REDIS_URL`](../README.md:151), because the application still uses the Redis client/backend for Redis-compatible services:

```bash
DB_ID="$(doctl databases list --format ID,Name --no-header | awk '$2=="lula-valkey" {print $1}')"
REDIS_URI="$(doctl databases connection "${DB_ID}" --format URI --no-header)"
kubectl create secret generic lula-valkey-secret \
  --namespace lula-orch \
  --from-literal=REDIS_URL="${REDIS_URI}" \
  --dry-run=client -o yaml | kubectl apply -f -
```

Then patch [`infra/k8s/deployment.yaml`](../infra/k8s/deployment.yaml) to inject `LG_CHECKPOINT_REDIS_URL` from this secret, or override it in the Kubernetes Secret `lula-secrets` directly. No application code rename is required: keep [`backend = "redis"`](../configs/runtime.prod.toml:59) and [`redis_url`](../configs/runtime.prod.toml:60) because Valkey is consumed through the existing Redis-compatible client path.

The `runtime.prod.toml` checkpoint config:

```toml
[checkpoint]
backend = "redis"
redis_url = "redis://redis:6379/0"   # override with LG_CHECKPOINT_REDIS_URL
redis_ttl_seconds = 86400
```

### Step 9 — Bootstrap ArgoCD

```bash
export REPO_URL=https://github.com/your-org/lula  # REPLACE with your Git remote

REPO_URL="${REPO_URL}" bash scripts/argocd_bootstrap.sh
```

The bootstrap script (see [`scripts/argocd_bootstrap.sh`](../scripts/argocd_bootstrap.sh)) does:
1. Creates `argocd` namespace
2. Installs ArgoCD stable from upstream manifests
3. Waits for `argocd-server` to become available (120 s timeout)
4. Installs ArgoCD Image Updater (semver image tracking)
5. Applies the `lula` AppProject ([`infra/k8s/argocd-project.yaml`](../infra/k8s/argocd-project.yaml))
6. Applies the `lula` Application ([`infra/k8s/argocd-app.yaml`](../infra/k8s/argocd-app.yaml)) with `REPO_URL` substituted
7. Applies the ArgoCD RBAC bindings ([`infra/k8s/argocd-rbac.yaml`](../infra/k8s/argocd-rbac.yaml))

Retrieve the ArgoCD admin password and log in:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d && echo

# Port-forward and log in
kubectl port-forward svc/argocd-server -n argocd 8080:443 &
argocd login localhost:8080 --username admin --password <PASSWORD> --insecure
```

Check sync status:

```bash
argocd app get lula
argocd app sync lula   # trigger immediate sync if needed
```

The ArgoCD `lula` AppProject enforces sync windows:

| Window | Schedule | Duration | Manual sync |
|---|---|---|---|
| Allow | Mon–Fri 09:00 UTC | 8 h | Yes |
| Deny (blackout) | Sat–Sun 00:00 UTC | 48 h | No |

For an emergency deployment during blackout: `argocd app sync lula --force`

### Step 10 — Apply Core Manifests (ArgoCD manages this automatically)

ArgoCD watches `infra/k8s/` on the `main` branch and auto-syncs. To apply manually or for initial verification:

```bash
kubectl apply -f infra/k8s/namespace.yaml
kubectl apply -f infra/k8s/gvisor-runtime-class.yaml
kubectl apply -f infra/k8s/deployment.yaml
kubectl apply -f infra/k8s/service.yaml
kubectl apply -f infra/k8s/runner-deployment.yaml
kubectl apply -f infra/k8s/runner-service.yaml
kubectl apply -f infra/k8s/hpa.yaml
kubectl apply -f infra/k8s/pdb.yaml
kubectl apply -f infra/k8s/network-policy.yaml
kubectl apply -f infra/k8s/ingress.yaml
```

### Step 11 — Configure DNS and TLS

Get the Load Balancer external IP:

```bash
kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

Point your domain's A record to this IP. Example (using `doctl`):

```bash
LB_IP=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

doctl compute domain records create your-domain.com \
  --record-type A \
  --record-name lula \
  --record-data "${LB_IP}" \
  --record-ttl 300
```

Edit the domain in [`infra/k8s/ingress.yaml`](../infra/k8s/ingress.yaml) before applying:

```yaml
tls:
  - hosts:
      - lula.your-domain.com   # REPLACE
    secretName: lula-orch-tls
rules:
  - host: lula.your-domain.com  # REPLACE
```

cert-manager issues the Let's Encrypt certificate automatically once DNS propagates (~1–5 min):

```bash
kubectl get certificate -n lula-orch lula-orch-tls -w
# Wait for READY = True
```

### Step 12 — Verify Deployment

```bash
# Check all pods are Running
kubectl get pods -n lula-orch -o wide

# Check HPA status
kubectl get hpa -n lula-orch

# Check PDB status
kubectl get pdb -n lula-orch

# Health check
curl -fsS https://lula.your-domain.com/healthz

# Smoke test with dry-run (eval framework)
cd eval && uv run python run.py --task tasks/canary.json --dry-run
```

Expected output from `/healthz`:

```json
{"status": "ok", "runner": "ok", "checkpoint": "ok"}
```

Runner health check (internal, via kubectl exec):

```bash
kubectl exec -n lula-orch \
  $(kubectl get pod -n lula-orch -l app=lula-runner -o name | head -1) \
  -- curl -fsS http://localhost:8088/healthz
```

### Step 13 — CI/CD Wiring

The release pipeline ([`.github/workflows/release.yml`](../.github/workflows/release.yml)) requires two GitHub repository secrets:

| Secret | Value |
|---|---|
| `DIGITALOCEAN_ACCESS_TOKEN` | DO API token with registry read/write |
| `DO_REGISTRY_NAME` | `lula-registry` |

On every `v*.*.*` Git tag push the pipeline:
1. Runs `cargo deny check` (supply-chain security gate)
2. Runs Python and Rust tests
3. Builds the Firecracker guest rootfs and attaches `rootfs.ext4` to the GitHub Release
4. Builds and pushes the container image to DOCR with semver tags
5. Runs Trivy CVE scan (fails on CRITICAL/HIGH unfixed)
6. Pins the image digest in [`infra/k8s/deployment.yaml`](../infra/k8s/deployment.yaml) and commits back to `main`
7. ArgoCD detects the manifest diff and rolls out within the polling interval (~3 min)

---

## 6. Secrets Reference

All secrets are stored in the `lula-secrets` Kubernetes Secret in the `lula-orch` namespace (see [`infra/k8s/secrets.yaml.example`](../infra/k8s/secrets.yaml.example)).

| Secret key | Description | How to generate / obtain |
|---|---|---|
| `LG_REMOTE_API_BEARER_TOKEN` | Bearer token for the public-facing orchestrator API | `openssl rand -hex 32` |
| `LG_RUNNER_API_KEY` | Shared key between orchestrator and runner (HTTP header auth) | `openssl rand -hex 32` |
| `DIGITAL_OCEAN_MODEL_ACCESS_KEY` | DO GenAI inference API key | [DO Console → AI/ML → API Keys](https://cloud.digitalocean.com/gen-ai) |
| `MODEL_ACCESS_KEY` | OpenAI / Anthropic API key (fallback provider) | OpenAI / Anthropic dashboards |

Additional secrets injected at runtime (not in `secrets.yaml.example` — add manually):

| Secret key | Description | How to generate / obtain |
|---|---|---|
| `LG_CHECKPOINT_REDIS_URL` | DO Managed Valkey connection string (Redis-compatible) | `DB_ID="$(doctl databases list --format ID,Name --no-header | awk '$2=="lula-valkey" {print $1}')" && doctl databases connection "${DB_ID}" --format URI` |
| `LG_AUTH_JWKS_URL` | JWKS endpoint for RS256 JWT validation | Your IdP (Auth0, Keycloak, Okta, etc.) |
| `LG_AUTH_SECRET` | HMAC secret for HS256 JWT mode | `openssl rand -hex 32` |
| `LG_RUNNER_APPROVAL_SECRET` | HMAC-SHA256 key for approval token signing/validation | `openssl rand -hex 32` |
| `LG_AUDIT_S3_BUCKET` | S3/Spaces bucket for audit trail JSONL export | Create in DO Spaces or AWS S3 |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | S3 / Spaces credentials for audit export | AWS IAM or DO Spaces HMAC keys |

Rotate any secret with:

```bash
kubectl -n lula-orch edit secret lula-secrets
# After saving:
kubectl -n lula-orch rollout restart deployment/lula-orch deployment/lula-runner
```

Security notes:
- Never commit `infra/k8s/secrets.yaml` to source control. It is in `.gitignore`.
- ArgoCD does not manage Secret objects — they must be applied manually or via an external secret manager (e.g., Doppler, HashiCorp Vault, External Secrets Operator).
- All secrets are redacted from structured log records by the `structlog` processor pipeline.

---

## 7. Firecracker Setup (Tier 3 Addendum)

### Node Requirements

Firecracker requires hardware virtualization (`/dev/kvm`) to be accessible inside the container. On DigitalOcean, this requires:
- Droplet size `s-8vcpu-16gb` or larger with KVM enabled (standard Ubuntu 22.04 Droplets support nested virtualization)
- OR dedicated bare-metal nodes via DO Bare Metal

Verify KVM availability on a node:

```bash
kubectl debug node/<node-name> -it --image=ubuntu:22.04 -- ls -la /dev/kvm
```

### Step 1 — Download vmlinux

The Firecracker-compatible kernel binary is not built in this repo. Download it from the official Firecracker releases:

```bash
ARCH=x86_64
FC_VERSION=v1.7.0
curl -fsSL \
  "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/vmlinux-${FC_VERSION}-${ARCH}.bin" \
  -o artifacts/vmlinux
```

### Step 2 — Build the Guest Rootfs

Build the `lula-guest-agent` Rust binary (compiled for `x86_64-unknown-linux-musl`) and package it into a 256 MiB Alpine-based ext4 image:

```bash
# Requires Docker with BuildKit
bash scripts/build_guest_rootfs.sh
# Output: artifacts/rootfs.ext4 (256 MiB)
```

The build pipeline ([`rs/guest-agent/Dockerfile.rootfs`](../rs/guest-agent/Dockerfile.rootfs)):
1. Compiles `lula-guest-agent` with musl target (statically linked, no glibc dependency)
2. Assembles a minimal Alpine rootfs with busybox
3. Creates `/sbin/init` that mounts proc/sysfs/devtmpfs then `exec`s `lula-guest-agent`
4. Packages the rootfs into a 256 MiB ext4 image via `mkfs.ext4 -d`

The `rootfs.ext4` artifact is also attached to every GitHub Release as `firecracker-rootfs` by the CI pipeline.

### Step 3 — Copy Assets to Nodes

Copy both assets to `/opt/lula/` on every Firecracker-capable node:

```bash
# For each Firecracker node, copy via a privileged DaemonSet or SSH
for NODE in $(kubectl get nodes -l sandbox=firecracker -o name); do
  kubectl debug "${NODE}" -it --image=ubuntu:22.04 -- bash -c "
    mkdir -p /host/opt/lula
    # Transfer files (adjust path as needed)
  "
done
```

Or use a bootstrap DaemonSet that pulls the assets from S3/Spaces on node startup:

```bash
# Example: upload to DO Spaces first
doctl spaces cp artifacts/rootfs.ext4 s3://lula-assets/firecracker/rootfs.ext4
doctl spaces cp artifacts/vmlinux     s3://lula-assets/firecracker/vmlinux
```

The runner-deployment mounts `/opt/lula/` from the host:

```yaml
volumes:
  - name: firecracker-assets
    hostPath:
      path: /opt/lula
      type: DirectoryOrCreate
```

### Step 4 — Configure Runner for Firecracker

Add a dedicated Firecracker node pool:

```bash
doctl kubernetes node-pool create "${DO_CLUSTER_NAME}" \
  --name firecracker-pool \
  --count 2 \
  --size s-8vcpu-16gb \
  --label sandbox=firecracker \
  --taint "sandbox=firecracker:NoSchedule"
```

Update [`infra/k8s/runner-deployment.yaml`](../infra/k8s/runner-deployment.yaml) to target the firecracker pool and set the sandbox tier:

```yaml
nodeSelector:
  sandbox: firecracker
env:
  - name: LG_RUNNER_SANDBOX_TIER
    value: "firecracker"
  - name: LG_RUNNER_ROOTFS_IMAGE
    value: "/opt/lula/rootfs.ext4"
  - name: LG_RUNNER_KERNEL_IMAGE
    value: "/opt/lula/vmlinux"
```

### Step 5 — Verify Firecracker Isolation

```bash
# Health check with sandbox tier assertion
curl -fsS http://<runner-internal-ip>:8088/healthz | jq '.sandbox_tier'
# Expected: "firecracker"

# Or via kubectl exec
kubectl exec -n lula-orch \
  $(kubectl get pod -n lula-orch -l app=lula-runner -o name | head -1) \
  -- curl -fsS http://localhost:8088/healthz
```

---

## 8. Monitoring and Observability

### Prometheus Scraping

Both pods expose Prometheus metrics via annotations in their deployment manifests:

| Component | Path | Port |
|---|---|---|
| `lula-orch` | `/metrics` | `8001` |
| `lula-runner` | `/metrics` | `8088` |

Install Prometheus + Grafana via the kube-prometheus-stack Helm chart:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false
```

Create a `PodMonitor` targeting both Lula components:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: lula-metrics
  namespace: monitoring
spec:
  namespaceSelector:
    matchNames: [lula-orch]
  selector:
    matchExpressions:
      - key: app
        operator: In
        values: [lula-orch, lula-runner]
  podMetricsEndpoints:
    - port: "8001"   # orchestrator
    - port: "8088"   # runner
```

### Key Metrics to Watch

| Metric | Component | Alert threshold | Notes |
|---|---|---|---|
| `lg_run_active_total` | orch | > 50 | Active concurrent agent runs |
| `lg_llm_latency_seconds` (p95) | orch | > 30 s | LLM inference latency |
| `lg_tool_call_error_rate` | runner | > 5% | Tool execution failure rate |
| `lg_approval_pending_total` | orch | > 10 (10 min) | Stalled approval queue |
| `lg_checkpoint_write_duration_seconds` | orch | > 2 s | Redis write latency |
| `container_memory_working_set_bytes` | both | > 1.8 Gi | Near memory limit |
| `kube_pod_container_status_restarts_total` | both | > 3/hr | CrashLoop detection |

### Grafana Dashboard

Import or configure panels for:
- Active runs timeseries with HPA replica count overlay
- LLM call latency histogram (p50/p95/p99) by provider
- Tool call error rate by tool type (`exec`, `apply_patch`, `read_file`)
- Approval queue depth
- Redis checkpoint write/read latency
- Pod CPU/memory vs HPA thresholds

### Suggested Alert Rules (Prometheus alerting rules)

```yaml
groups:
  - name: lula.rules
    rules:
      - alert: LulaHighErrorRate
        expr: rate(lg_tool_call_error_rate[5m]) > 0.05
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Tool call error rate > 5%"

      - alert: LulaHighActiveRuns
        expr: lg_run_active_total > 50
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "More than 50 concurrent runs"

      - alert: LulaLLMLatencyHigh
        expr: histogram_quantile(0.95, rate(lg_llm_latency_seconds_bucket[5m])) > 30
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "LLM p95 latency > 30s"

      - alert: LulaPodCrashLooping
        expr: rate(kube_pod_container_status_restarts_total{namespace="lula-orch"}[1h]) > 3
        labels:
          severity: critical
        annotations:
          summary: "Pod crash-looping in lula-orch namespace"
```

### OpenTelemetry Tracing

Configure OTLP export in the orchestrator via environment variables:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://tempo.your-grafana-cloud.io:4317
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64-encoded-token>"
OTEL_SERVICE_NAME=lula-orch
OTEL_RESOURCE_ATTRIBUTES="deployment.environment=production,k8s.cluster.name=lula-prod"
```

Each LangGraph node emits a span. The runner emits child spans for tool dispatch, sandbox invocation, and approval gate evaluation.

### Log Aggregation

All components emit structured JSON logs via `structlog` (orchestrator) and `tracing-subscriber` JSON format (runner). Route to Loki or DataDog:

```bash
# Loki (via Promtail or Grafana Agent)
helm install loki-stack grafana/loki-stack \
  --namespace monitoring \
  --set promtail.enabled=true \
  --set loki.enabled=true
```

Log query example (Grafana Explore / LogQL):

```logql
{namespace="lula-orch", container="lula-orch"}
  | json
  | level = "error"
  | line_format "{{.run_id}} {{.event}} {{.message}}"
```

---

## 9. Scaling Guidelines

### HPA Configuration (from [`infra/k8s/hpa.yaml`](../infra/k8s/hpa.yaml))

| Component | Min replicas | Max replicas | Scale-up trigger | Scale-down stabilization |
|---|---|---|---|---|
| `lula-orch` | 2 | 10 | CPU > 70% OR memory > 80% | 300 s (5 min) |
| `lula-runner` | 2 | 20 | CPU > 60% | 120 s (2 min) |

Scale-up burst policies:
- **Orchestrator:** +2 pods per 60 s, stabilization window 30 s
- **Runner:** +4 pods per 60 s, stabilization window 15 s

### Scaling Decision Guide

**Orchestrator is the bottleneck when:**
- CPU > 70% at current replica count (I/O-bound LLM calls, async await)
- LLM latency p95 is high but runner latency is low
- Active run queue depth is growing

**Runner is the bottleneck when:**
- CPU > 60% at current replica count (CPU-bound: subprocess exec, Firecracker VM launch)
- Tool call queue depth is growing
- Runner latency spikes while LLM latency is stable

### Scale-Down Caution

The 5-minute scale-down stabilization for the orchestrator prevents replica thrashing between LLM call bursts (async I/O means CPU drops rapidly after each batch). The 2-minute stabilization for the runner is shorter because runner pods are stateless — checkpoints survive runner pod death via Redis.

### Manual Scaling

For planned load events (bulk eval runs, demos), scale manually ahead of time:

```bash
kubectl scale deployment/lula-orch -n lula-orch --replicas=6
kubectl scale deployment/lula-runner -n lula-orch --replicas=10
# HPA will take back control once load stabilizes
```

### Node Pool Scaling

Expand node pools for sustained load above HPA max:

```bash
doctl kubernetes node-pool update "${DO_CLUSTER_NAME}" gvisor-pool \
  --count 4

doctl kubernetes node-pool update "${DO_CLUSTER_NAME}" default-pool \
  --count 4
```

---

## 10. Disaster Recovery

### Checkpoint Durability

LangGraph run state is checkpointed to Redis on every graph node transition. A run that is interrupted mid-execution (pod crash, node eviction, OOM kill) can be resumed from the last checkpoint by resubmitting the same `thread_id`.

Valkey persistence is enabled on DO Managed Valkey by default. Check the current DO retention and restore guarantees for your selected plan and region at provisioning time.

Checkpoint TTL: `86400` s (24 h) — configurable via `LG_CHECKPOINT_REDIS_TTL_SECONDS`.

### Run Store Durability

The `run_store_path` SQLite file (`artifacts/remote-api/runs.sqlite`) is ephemeral on pod filesystem. For durability either:
- Mount a PersistentVolumeClaim (DO Block Storage) at `artifacts/`
- Or configure the audit trail S3/GCS export and treat the run store as a cache only

### Healing Loop

The orchestrator implements an automatic healing loop (`py/src/lg_orch/healing_loop.py`): it detects runs that have been stuck in `pending` state beyond a configurable threshold and re-queues them. This recovers from transient runner failures without operator intervention.

### Git Snapshot / Undo

The runner creates a git snapshot before every `apply_patch` mutation. If a run fails verification, the orchestrator can issue an undo that reverts to the last snapshot. Snapshot refs survive pod restarts because they are stored in the git object database of the workspace.

### Pod Disruption Budgets

Both `lula-orch-pdb` and `lula-runner-pdb` enforce `minAvailable: 1` (see [`infra/k8s/pdb.yaml`](../infra/k8s/pdb.yaml)). Node drain operations during cluster upgrades will not simultaneously evict all pods.

### Multi-AZ Topology Spread

Spread runner pods across all three NYC3 availability zones to tolerate single-zone failure:

```yaml
# Add to runner-deployment.yaml spec.template.spec
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app: lula-runner
```

### Rollback Procedure

**Option A — Git revert (preferred, ArgoCD auto-syncs):**

```bash
git revert <bad-commit-sha>
git push origin main
# ArgoCD reconciles within ~3 minutes
```

**Option B — ArgoCD history rollback:**

```bash
argocd app history lula
argocd app rollback lula <REVISION-ID>
# Re-enable auto-sync after:
argocd app set lula --sync-policy automated
```

**Option C — Emergency kubectl rollout undo:**

```bash
kubectl -n lula-orch rollout undo deployment/lula-orch
kubectl -n lula-orch rollout undo deployment/lula-runner
# Note: causes ArgoCD drift — re-align by pushing a corrective commit
```

---

## 11. Cost Optimization

### Use Spot / Preemptible Nodes for Runner Pool

The runner is stateless at the pod level — run state lives in Valkey-backed Redis-compatible checkpoints. Runner pods can safely run on DO Spot Droplets (when available) or use DOKS auto-scaling node pools that scale to zero overnight.

```bash
# Add a spot node pool for burst runner capacity
doctl kubernetes node-pool create "${DO_CLUSTER_NAME}" \
  --name runner-spot-pool \
  --count 0 \
  --size s-4vcpu-8gb \
  --label sandbox=gvisor \
  --taint "sandbox=gvisor:NoSchedule" \
  --auto-scale \
  --min-nodes 0 \
  --max-nodes 10
```

### Reserved Droplets for Baseline Orchestrator Capacity

The orchestrator minimum 2 replicas represent a constant baseline load. Purchase DO Reserved Droplets for the default node pool to save ~20% vs on-demand pricing for committed annual usage.

### Scale Runner Pool to Zero Overnight

If workloads are business-hours only, configure the runner HPA minimum to 0 outside business hours using a CronJob or KEDA scheduled scaling:

```bash
# Scale down at 20:00 UTC
kubectl patch hpa lula-runner-hpa -n lula-orch \
  -p '{"spec":{"minReplicas":0}}'

# Scale up at 08:00 UTC
kubectl patch hpa lula-runner-hpa -n lula-orch \
  -p '{"spec":{"minReplicas":2}}'
```

### Audit Trail Archival

The audit JSONL is written to S3/Spaces. Use lifecycle policies to move objects to cheaper storage tiers:

- S3 Standard → S3 Intelligent-Tiering after 7 days
- S3 Intelligent-Tiering → S3 Glacier after 30 days
- DO Spaces: configure object lifecycle via `s3cmd` or AWS CLI:

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket lula-audit \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "archive-old-audits",
      "Status": "Enabled",
      "Filter": {"Prefix": "audit/"},
      "Transitions": [
        {"Days": 30, "StorageClass": "GLACIER"}
      ]
    }]
  }'
```

### DOCR Garbage Collection

Run registry garbage collection weekly to reclaim storage from untagged image layers:

```bash
doctl registry garbage-collection start lula-registry
```

Do **not** run garbage collection while a deployment is in progress — it may delete layers currently being pulled.

### Summary Cost Table

| Configuration | Monthly cost estimate | Notes |
|---|---|---|
| Tier 1 — App Platform | ~$5–$20 | No HA, no gVisor |
| Tier 2 — DOKS baseline (2+2 nodes) | ~$186 | Before autoscale |
| Tier 2 — DOKS typical (4+4 nodes) | ~$300–$350 | Mid-scale production |
| Tier 2 — DOKS full scale (10+20 pods) | ~$1,200–$1,500 | Peak autoscale |
| Tier 3 — Firecracker add-on | +$192–$384 | 2–4 dedicated KVM nodes |
| Savings with Reserved Droplets (1-yr) | −20% on node cost | Orchestrator baseline only |
| Savings with overnight runner scale-to-zero | −30–40% runner cost | Business-hours workloads |
