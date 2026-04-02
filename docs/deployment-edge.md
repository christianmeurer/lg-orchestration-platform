# Edge Deployment Guide

Deploy Lula on a single-node local or air-gapped environment using k3s, Ollama, and local container images.

## Prerequisites

- Linux host with at least 4 CPU cores and 8 GB RAM
- Docker installed (for building images)
- `kubectl` and `helm` CLI tools

## 1. Install k3s (single-node cluster)

```bash
# Install k3s without Traefik (we use NodePort for edge)
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable=traefik" sh -

# Configure kubectl
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Verify the cluster is running
kubectl get nodes
```

## 2. Install Ollama (local LLM provider)

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull the model used by the edge profile
ollama pull llama3.2

# Verify Ollama is serving
curl http://localhost:11434/api/tags
```

Ollama listens on `http://localhost:11434` by default. The orchestrator connects to it via the `ollama-llama3.2` model route configured in `values-edge.yaml`.

## 3. Build local Docker images

```bash
cd /path/to/Lula

# Build the Python orchestrator image
docker build -t lula:latest -f py/Dockerfile .

# Build the Rust runner image
docker build -t lula-runner:latest -f rs/Dockerfile .
```

k3s uses containerd and can import images directly:

```bash
# Import images into k3s containerd
sudo k3s ctr images import <(docker save lula:latest)
sudo k3s ctr images import <(docker save lula-runner:latest)
```

## 4. Create the namespace and secrets

```bash
kubectl create namespace lula-orch

# If you need to override any secrets beyond the defaults in values-edge.yaml,
# create a Kubernetes secret manually:
# kubectl create secret generic lula-secrets -n lula-orch \
#   --from-literal=RUNNER_API_KEY=edge-runner-key \
#   --from-literal=HMAC_SECRET=edge-hmac
```

## 5. Deploy with Helm

```bash
helm install lula charts/lula \
  -n lula-orch \
  -f charts/lula/values-edge.yaml
```

## 6. Verify the deployment

```bash
# Check pods are running
kubectl get pods -n lula-orch

# Check orchestrator health
kubectl exec -n lula-orch \
  $(kubectl get pods -n lula-orch -l app=lula-orch -o jsonpath='{.items[0].metadata.name}') \
  -- curl -sf http://localhost:8001/healthz

# Check runner health
kubectl exec -n lula-orch \
  $(kubectl get pods -n lula-orch -l app=lula-runner -o jsonpath='{.items[0].metadata.name}') \
  -- curl -sf http://localhost:8088/healthz
```

The orchestrator is accessible via NodePort. Find the assigned port:

```bash
kubectl get svc -n lula-orch lula-orch-service
```

## 7. Connect to the orchestrator

```bash
# Get the NodePort
NODE_PORT=$(kubectl get svc -n lula-orch lula-orch-service \
  -o jsonpath='{.spec.ports[0].nodePort}')

# Test the API
curl http://localhost:$NODE_PORT/healthz
```

## Upgrading

```bash
# Rebuild images, reimport, then upgrade
helm upgrade lula charts/lula \
  -n lula-orch \
  -f charts/lula/values-edge.yaml
```

## Troubleshooting

- **Ollama not reachable**: Ensure Ollama is running (`systemctl status ollama`) and listening on the expected address. If running inside k3s, you may need to use the host network IP instead of `localhost`.
- **Image pull errors**: Verify images are imported into k3s containerd with `sudo k3s ctr images ls | grep lula`.
- **PVC not binding**: The edge profile uses `local-path` storage class, which is included by default in k3s. Check with `kubectl get sc`.
