# External Secrets Operator Integration

These manifests integrate Lula with the [External Secrets Operator](https://external-secrets.io/) 
for automated secret management.

## Prerequisites

1. Install ESO in your cluster:
   ```bash
   helm repo add external-secrets https://charts.external-secrets.io
   helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace
   ```

2. Configure your secret provider (AWS Secrets Manager, HashiCorp Vault, GCP Secret Manager, etc.)

3. Update `secret-store.yaml` with your provider configuration.

4. Create the secrets in your provider with keys matching `remoteRef.key` values.

## Usage

```bash
kubectl apply -f secret-store.yaml
kubectl apply -f external-secret.yaml
```

The ESO will create a Kubernetes Secret named `lula-secrets` and keep it synced with your provider.

Update the orchestrator and runner deployments to reference this secret instead of manually managed ones.
