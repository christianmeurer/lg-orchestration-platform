# DigitalOcean Deployment — LG-Orchestration-Platform

This document describes how to build, push, and run the platform on DigitalOcean using either **App Platform** (recommended) or a **Droplet** (lower cost, manual TLS).

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

## Two deployment targets

| Target | `DO_DEPLOY_TARGET` | HTTPS | Cost |
|---|---|---|---|
| App Platform (recommended) | `app` (default) | Automatic (`*.ondigitalocean.app`) | ~$5/mo (Basic) |
| Droplet | `droplet` | Manual reverse proxy required | ~$6/mo (1 vCPU / 2 GB) or ~$12/mo (2 vCPU / 4 GB) |

Both targets share the same DOCR push step.

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
| Secret env vars | `LG_REMOTE_API_BEARER_TOKEN`, `LG_RUNNER_API_KEY`, `DIGITAL_OCEAN_MODEL_ACCESS_KEY`, `MODEL_ACCESS_KEY` | Must be set via console or `doctl apps update` after creation |

### Create the app (first deploy)

```sh
doctl apps create --spec infra/do/app.yaml
```

### Update an existing app

```sh
doctl apps update <APP_ID> --spec infra/do/app.yaml
```

The deploy script handles create-or-update detection automatically.

### Setting secret environment variables

Secret vars are **not** stored in `app.yaml` (which is committed to source control). Set them after creation:

```sh
doctl apps update <APP_ID> --spec infra/do/app.yaml
# Then in the DO console: App → Settings → Environment Variables → edit secrets
```

Or use the `do_deploy.sh` `--set-secrets` pattern described in [Updating / rolling deploys](#updating--rolling-deploys).

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

## Quick-start reference

```sh
# One-time setup
doctl auth init
doctl registry create lula-orch --region nyc3

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
