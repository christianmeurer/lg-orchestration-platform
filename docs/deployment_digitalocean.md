# DigitalOcean Deployment — LG-Orchestration-Platform

This document describes how to build, push, and run the platform on DigitalOcean using **App Platform**, a **Droplet**, or the split **App Platform + DOKS runner** production topology.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| `doctl` ≥ 1.100 | `doctl auth init` must succeed |
| Docker ≥ 24 | Must be able to build `linux/amd64` images |
| Bash | Required by `do_deploy.sh`; Windows users invoke via WSL or Git Bash |
| `DO_REGISTRY` env var | DOCR registry name you own or will create |
| Secret env vars | See [Environment variables](#environment-variables) below |

Authenticate once:

```sh
doctl auth init
```

---

## Deployment Targets

The platform supports multiple deployment strategies depending on your operational maturity and scaling needs:

| Target | Description | HTTPS | Cost |
|---|---|---|---|
| **Docker Compose** | Fastest local/Droplet startup | Manual reverse proxy | Low / Free |
| **Helm Chart** | Scalable K8s deployment (simplifies DOKS) | Auto (cert-manager) | Medium/High |
| **App Platform** | Fully managed DO service (recommended) | Automatic | ~$5/mo |
| **Droplet** | Manual Docker-based deployment | Manual reverse proxy | ~$6/mo |

---

## Docker Compose (New/Simplified Local & Droplet)

For a one-command setup without Kubernetes overhead, use the provided `docker-compose.yml`:

```sh
# Copy .env example or set variables
export LG_REMOTE_API_BEARER_TOKEN="your-secret"
docker compose up -d
```
This deploys both the orchestrator and the rust runner networked securely together.

---

## Helm Chart Deployment (New/Simplified Kubernetes)

Instead of manually applying 5+ manifests and substituting secrets manually, use the provided Helm chart in `charts/lula/`.

```sh
# Override values or pass secrets directly
helm upgrade --install lula ./charts/lula \
  --namespace lula-orch \
  --create-namespace \
  --set secrets.remoteApiBearerToken="your-secret-token" \
  --set secrets.digitalOceanModelAccessKey="do-model-key"
```

---

## DigitalOcean Container Registry (DOCR)

### Create the registry (once)

```sh
doctl registry create lula-orch --region nyc3
```

The registry URL is `registry.digitalocean.com/<registry-name>`. The deploy script handles login, build, and push automatically. Do **not** run `doctl registry garbage-collection` while a deployment is in progress — it may delete layers in use.

### Authenticate Docker to DOCR

```sh
doctl registry login
```

The deploy script calls this automatically. To log in manually:

```sh
doctl registry docker-config | docker login registry.digitalocean.com --username <token> --password-stdin
```

---

## App Platform spec (`infra/do/app.yaml`)

The spec file at [`infra/do/app.yaml`](../infra/do/app.yaml) defines the App Platform service. Key fields:

| Field | Value | Notes |
|---|---|---|
| `region` | `nyc3` | Overridable in the spec |
| `instance_size_slug` | `apps-s-1vcpu-1gb` | Basic — adequate for personal use |
| `http_port` | `8001` | Matches `PORT` env var |
| `health_check.http_path` | `/healthz` | Remote API exposes this |
| Secret env vars | `LG_REMOTE_API_BEARER_TOKEN`, `LG_RUNNER_API_KEY`, `DIGITAL_OCEAN_MODEL_ACCESS_KEY`, `MODEL_ACCESS_KEY`, `LG_CHECKPOINT_REDIS_URL` | Must be set via console or injected by deployment automation |

### Create the app (first deploy)

```sh
doctl apps create --spec infra/do/app.yaml
```

### Update an existing app

```sh
doctl apps update <APP_ID> --spec infra/do/app.yaml
```

The deploy script handles create-or-update detection automatically.

### Current production topology note

The repository currently supports two distinct DigitalOcean production shapes:

1. **Combined App Platform / Droplet image** — orchestrator and runner launched together via [`scripts/start_remote_stack.sh`](../scripts/start_remote_stack.sh).
2. **Split production topology** — App Platform hosts the orchestrator UI/API while the hardened runner is deployed separately on DOKS via [`scripts/do_deploy_k8s.sh`](../scripts/do_deploy_k8s.sh) and referenced through `LG_RUNNER_BASE_URL`.

If you are using the split topology, the browser UI is served from the App Platform app and the runner health checks happen against the DOKS runner service.

### Setting secret environment variables

Secret vars are **not** stored in `app.yaml` (which is committed to source control). Set them after creation or use the planned one-shot deploy flow that generates and wires them automatically:

```sh
doctl apps update <APP_ID> --spec infra/do/app.yaml
# Then in the DO console: App → Settings → Environment Variables → edit secrets
```

The current [`scripts/do_deploy.sh`](../scripts/do_deploy.sh) updates the App Platform spec but still expects the secret values themselves to be managed out-of-band.

---

## Droplet target

### First deploy

The deploy script creates a `s-1vcpu-2gb` Ubuntu 22.04 Droplet, opens the API port, and injects a cloud-init user-data script that:

1. Installs `docker.io`
2. Authenticates to DOCR with `doctl registry docker-config`
3. Pulls the image
4. Starts the container with `--restart unless-stopped`
5. Health-polls `http://127.0.0.1:$PORT/healthz` for up to 120 seconds

### Subsequent deploys

The script SSHes into the existing Droplet and runs:

```sh
docker pull <image>
docker rm -f <app-name>
docker run -d --restart unless-stopped ...
```

### TLS on a Droplet

App Platform provides TLS automatically. For a Droplet you must add a reverse proxy:

```sh
# Minimal nginx + certbot example (run once on the Droplet)
sudo apt-get install -y nginx certbot python3-certbot-nginx
sudo certbot --nginx -d <your-domain>
```

Then proxy `443 → 127.0.0.1:8001`. Until TLS is configured, set `LG_REMOTE_API_TRUST_FORWARDED_HEADERS=false` and restrict access by IP if possible.

---

## Environment variables

| Variable | Required | Target | Description |
|---|---|---|---|
| `DO_REGISTRY` | **Yes** | both | DOCR registry name (e.g. `lula-orch`) |
| `DO_APP_NAME` | No (default `lula-orch`) | both | App name and image repository |
| `DO_REGION` | No (default `nyc3`) | both | DigitalOcean region slug |
| `DO_DEPLOY_TARGET` | No (default `app`) | — | `app` or `droplet` |
| `DO_DROPLET_SSH_KEY` | Droplet only | droplet | SSH key fingerprint or ID for Droplet access |
| `LG_PROFILE` | No (default `prod`) | both | Config profile; selects `configs/runtime.<profile>.toml` |
| `PORT` | No (default `8001`) | both | Port the remote API listens on (public) |
| `LG_REMOTE_API_AUTH_MODE` | No (default `bearer` if token present, else `off`) | both | `bearer` or `off` |
| `LG_REMOTE_API_BEARER_TOKEN` | Yes if `AUTH_MODE=bearer` | both | Bearer token for API auth — treat as secret |
| `LG_REMOTE_API_TRUST_FORWARDED_HEADERS` | No (default `true` for App Platform, `false` for Droplet) | both | Trust `X-Forwarded-For` and `X-Forwarded-Proto` |
| `LG_RUNNER_API_KEY` | No | both | API key between Python orchestrator and Rust runner (internal) |
| `MODEL_ACCESS_KEY` | No | both | Generic model provider access key |
| `DIGITAL_OCEAN_MODEL_ACCESS_KEY` | No | both | DigitalOcean GenAI model access key |

---

## Healthcheck

The remote API exposes `/healthz` on `$PORT` (default `8001`). App Platform polls it automatically per `infra/do/app.yaml`. For Droplet deployments the deploy script polls it locally before exiting.

The health payload currently returns:

```json
{"ok": true}
```

Manual check:

```sh
curl -fsS https://<live-url>/healthz
# or for Droplet:
curl -fsS http://<public-ip>:8001/healthz
```

---

## TLS / HTTPS

- **App Platform**: TLS is provisioned automatically. The app is reachable at `https://<name>-<id>.ondigitalocean.app`. Set `LG_REMOTE_API_TRUST_FORWARDED_HEADERS=true`.
- **Droplet**: HTTP only by default. Add nginx + certbot (see [Droplet target](#droplet-target) above). Set `LG_REMOTE_API_TRUST_FORWARDED_HEADERS=false` until a trusted proxy is in place.

### Browser UI path

The production browser UI is the SOTA 2026 run console served by the API and available at:

```sh
https://<app-domain>/app/
```

When bearer auth is enabled, open it with the tokenized URL form:

```sh
https://<app-domain>/app/?access_token=<LG_REMOTE_API_BEARER_TOKEN>
```

The UI persists the token in browser local storage and uses the same token for both REST calls and authenticated SSE stream setup.

---

## Updating / rolling deploys

### App Platform

```sh
# Re-push a new image tag and update the app spec
DO_REGISTRY=lula-orch DO_APP_NAME=lula-orch bash scripts/do_deploy.sh <new-tag>
```

App Platform performs a zero-downtime rolling replacement automatically once the new image is pushed and the app is updated.

### Droplet

```sh
DO_REGISTRY=lula-orch DO_DEPLOY_TARGET=droplet bash scripts/do_deploy.sh <new-tag>
```

---

## 4. DOKS + gVisor (Kubernetes hardened deployment)

### Why gVisor

The Rust runner executes arbitrary tool invocations (shell commands, file system ops) on behalf of LLM agents. On App Platform and plain Docker the runner shares the host kernel — a compromised tool invocation can escape the container via kernel exploits. gVisor interposes every syscall through its own user-space kernel (`runsc`), so the host kernel is never directly reachable from inside the runner container.

The orchestrator (Python API) does **not** need gVisor — only the runner pod is scheduled on gVisor nodes, reducing the overhead to where it matters.

### Prerequisites

| Requirement | Notes |
|---|---|
| `doctl` ≥ 1.100 | `doctl auth init` must succeed |
| `kubectl` | Configured by the deploy script via `doctl kubernetes cluster kubeconfig save` |
| cert-manager | Install once per cluster: `kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml` |
| nginx ingress controller | Install once: `kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/cloud/deploy.yaml` |
| `DO_REGISTRY` env var | DOCR registry name |
| Secret env vars | See [Environment variables](#environment-variables) |

### Step-by-step

#### 1. Create the cluster and gVisor node pool

```sh
export DO_REGISTRY=lula-orch
bash scripts/do_deploy_k8s.sh
```

The script creates:
- A **default node pool** (2 × `s-2vcpu-4gb`) for the control plane / orchestrator workloads.
- A **`gvisor-pool`** (2 × `s-2vcpu-4gb`) tainted `sandbox=gvisor:NoSchedule` and labeled `sandbox=gvisor`.

#### 2. Install gVisor on the node pool (automatic via DaemonSet)

`infra/k8s/gvisor-installer.yaml` deploys a DaemonSet to every `sandbox=gvisor` node. The privileged init container:

1. Downloads `runsc` from the official gVisor release bucket.
2. Installs it to `/usr/local/sbin/runsc`.
3. Patches `/etc/containerd/config.toml` to register the `runsc` runtime handler.
4. Restarts `containerd` via `nsenter` into the host PID namespace.

The main container is a pause image that keeps the DaemonSet Pod running so Kubernetes can track node readiness.

#### 3. Apply manifests

```sh
# After cluster is up and gVisor DaemonSet is running:
kubectl apply -f infra/k8s/gvisor-runtime-class.yaml
kubectl apply -f infra/k8s/secrets.yaml        # EDIT secrets first!
kubectl apply -f infra/k8s/deployment.yaml
kubectl apply -f infra/k8s/service.yaml
kubectl apply -f infra/k8s/ingress.yaml
```

The deploy script applies them all in one pass.

#### 4. Set secrets

Edit `infra/k8s/secrets.yaml` and replace every `REPLACE_ME` value before applying:

```sh
# Edit the file, then:
kubectl apply -f infra/k8s/secrets.yaml
kubectl rollout restart deployment/lula-orch -n lula-orch
```

#### 5. Set DNS

Point your domain's A record to the LoadBalancer IP printed by the deploy script:

```sh
kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

cert-manager will issue a Let's Encrypt certificate automatically once DNS propagates.

### `runtimeClassName: gvisor` — what it means

Setting `runtimeClassName: gvisor` in `infra/k8s/deployment.yaml` instructs the kubelet to create the pod's container using the `runsc` OCI runtime instead of `runc`. Every syscall from the container is intercepted by gVisor's user-space kernel. The container **cannot** directly reach the host kernel, making kernel CVE exploitation significantly harder.

Without this field the pod falls back to `runc` (standard container isolation). The field is enforced — if the RuntimeClass `gvisor` does not exist on the node, the pod fails to schedule.

### Cost guidance

| Configuration | Nodes | Size | Cost/mo (approx) |
|---|---|---|---|
| Minimum viable (1+1) | 1 default + 1 gVisor | `s-2vcpu-4gb` | ~$24 |
| Recommended (2+2) | 2 default + 2 gVisor | `s-2vcpu-4gb` | ~$48 |
| DOCR Starter | — | 1 repo / 500 MB | Free |

### Security posture comparison

| Dimension | App Platform | Droplet (Docker) | DOKS + gVisor |
|---|---|---|---|
| Kernel isolation | Shared (managed) | Shared | gVisor user-space kernel per pod |
| Kernel CVE exposure | Low (DO-managed) | High | Very low |
| Container escape risk | Medium | High | Low |
| Managed TLS | Yes | No (manual nginx+certbot) | Yes (cert-manager) |
| Rolling deploy | Yes (zero-downtime) | No (brief downtime) | Yes (Kubernetes rollout) |
| Ops complexity | Low | Medium | High |
| Cost/mo | ~$5 | ~$6–12 | ~$24–48 |
| Best for | Dev / personal | Cost-sensitive | Production / regulated |

The script SSHes in, pulls the new image, stops the old container, and starts the new one. There is a brief (~5 s) downtime window during container replacement.

---

## Cost guidance

| Option | Size | vCPU | RAM | Cost/mo |
|---|---|---|---|---|
| App Platform Basic | `apps-s-1vcpu-1gb` | 1 | 1 GB | ~$5 |
| Droplet Basic | `s-1vcpu-2gb` | 1 | 2 GB | ~$6 |
| Droplet Standard | `s-2vcpu-4gb` | 2 | 4 GB | ~$12 |
| DOCR Starter | — | — | 1 repo, 500 MB | Free |

App Platform is recommended for simplicity (managed TLS, rolling deploys, zero infra management). Droplet is preferred if you need direct SSH access, GPU attachment, or want to minimise cost at the expense of manual TLS setup.

---

## Enabling Persistent Workspace

By default, the runner uses an `emptyDir` volume for `/workspace`. This is wiped on pod restart.

To enable persistence:

1. Apply the PVC:
   ```bash
   kubectl apply -f infra/k8s/workspace-pvc.yaml
   ```

2. Update the Helm chart:
   ```bash
   helm upgrade lula ./charts/lula -n lula-orch --reuse-values \
     --set runner.workspace.persistent=true
   ```

Note: `ReadWriteOnce` PVCs can only be mounted by one pod at a time. If you have multiple runner replicas, use `ReadWriteMany` with a shared storage class (e.g., NFS or DO Spaces).

---

## Quick-start reference

Validation note for this repository change set: the deployment scripts and startup shell wrappers were syntax-checked, and the focused Python/UI tests passed, but local image build validation could not be completed in this workspace because the local Docker daemon was unavailable.

```sh
# One-time setup
doctl auth init
doctl registry create lula-orch --region nyc3

# Preferred: one-shot App Platform deploy with generated Lula secrets
export DO_REGISTRY=lula-orch
export DIGITAL_OCEAN_MODEL_ACCESS_KEY=<secret>
bash scripts/do_deploy_one_shot.sh

# Deploy (App Platform)
export DO_REGISTRY=lula-orch
export LG_REMOTE_API_BEARER_TOKEN=<secret>
export LG_RUNNER_API_KEY=<secret>
export DIGITAL_OCEAN_MODEL_ACCESS_KEY=<secret>
bash scripts/do_deploy.sh

# Deploy (Droplet)
export DO_DEPLOY_TARGET=droplet
export DO_DROPLET_SSH_KEY=<fingerprint>
bash scripts/do_deploy.sh
```

After deployment, open the browser UI at:

```sh
https://<app-domain>/app/?access_token=<LG_REMOTE_API_BEARER_TOKEN>
```
