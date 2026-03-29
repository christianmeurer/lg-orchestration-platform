# Container Registry Setup

This document describes how to configure the DigitalOcean Container Registry (DOCR)
for the Lula release pipeline and how image digest pinning works.

---

## Prerequisites

### 1. Create the DOCR registry

Create a registry named `lula-registry` in your DigitalOcean account:

```bash
doctl registry create lula-registry --subscription-tier basic
```

A custom name is allowed; update `DO_REGISTRY_NAME` accordingly (see below).

### 2. Configure GitHub repository secrets

Navigate to **Settings → Secrets and variables → Actions** in the GitHub repository
and add the following secrets:

| Secret name                  | Value                                                          |
|------------------------------|----------------------------------------------------------------|
| `DO_REGISTRY_NAME`           | Registry name, e.g. `lula-registry`                           |
| `DIGITALOCEAN_ACCESS_TOKEN`  | A DO API token with **registry** read/write scope              |

The token requires at minimum the `registry` scope. Generate one at
<https://cloud.digitalocean.com/account/api/tokens>.

### 3. Create the in-cluster pull secret

The Kubernetes manifests reference an `imagePullSecret` named `docr-secret`.
Create it once per namespace:

```bash
doctl registry kubernetes-manifest | sed "s/name: registry-.*/name: docr-secret/" | kubectl apply -n lula-orch -f -
```

Or manually:

```bash
kubectl create secret docker-registry docr-secret \
  --docker-server=registry.digitalocean.com \
  --docker-username=<DO_ACCESS_TOKEN> \
  --docker-password=<DO_ACCESS_TOKEN> \
  -n lula-orch
```

---

## Release Process

Releases are triggered by pushing a semver tag. The
[`release.yml`](../.github/workflows/release.yml) workflow executes four stages:

1. **Tests** — Python (`pytest`) and Rust (`cargo test`) suites must pass.
2. **Build and push** — Docker Buildx builds the multi-stage
   [`Dockerfile`](../Dockerfile) and pushes two tags to DOCR:
   - `registry.digitalocean.com/<DO_REGISTRY_NAME>/lula:<major>.<minor>.<patch>`
   - `registry.digitalocean.com/<DO_REGISTRY_NAME>/lula:<major>.<minor>`
3. **Trivy scan** — A CVE scan runs against the pushed image. `CRITICAL` or `HIGH`
   unfixed vulnerabilities abort the workflow and block the manifest update.
4. **Manifest update** — `yq` pins the exact image digest (e.g.
   `registry.digitalocean.com/lula-registry/lula@sha256:<digest>`) into
   `infra/k8s/deployment.yaml` and commits the change back to `main`.

ArgoCD detects the manifest commit and syncs the cluster automatically (see
[`docs/gitops.md`](./gitops.md)).

### Trigger a release

```bash
git tag v1.0.0
git push origin v1.0.0
```

The workflow begins immediately. Monitor progress in
**Actions → Release** on GitHub.

---

## Manual Image Update (Emergency Procedure)

Use this only when a hotfix must bypass the normal release workflow.

```bash
kubectl set image deployment/lula-orch \
  lula-orch=registry.digitalocean.com/lula-registry/lula@sha256:<digest> \
  -n lula-orch

kubectl set image deployment/lula-runner \
  lula-runner=registry.digitalocean.com/lula-registry/lula@sha256:<digest> \
  -n lula-orch
```

> **Warning:** A `kubectl set image` command creates GitOps drift — the live cluster
> diverges from the manifests in Git. ArgoCD will detect and flag this as `OutOfSync`.
> After verifying the hotfix, update `infra/k8s/deployment.yaml` and
> `infra/k8s/runner-deployment.yaml` with the pinned digest and push to `main` to
> restore sync.

---

## Image Scanning

Continuous CVE scanning runs on every push to `main` via
[`.github/workflows/image-scan.yml`](../.github/workflows/image-scan.yml) using
[Trivy](https://github.com/aquasecurity/trivy).

Scan results are uploaded to GitHub Advanced Security and appear under
**Security → Code scanning** in the repository. Any `CRITICAL` or `HIGH` finding
with an available fix will generate a code-scanning alert.
