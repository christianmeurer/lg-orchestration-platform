# DigitalOcean Troubleshooting & Fixes Post-Mortem (2026)

## Overview
This document serves as a post-mortem and knowledge base for the fixes applied to the Lula project during the DigitalOcean deployment debugging session.

## Issues Addressed

### 1. NetworkPolicy Port Mismatches
**Symptoms:** Pods were unable to communicate with each other or accept incoming traffic from the ingress controller.
**Root Cause:** The Kubernetes `NetworkPolicy` and `Service` definitions had mismatched ports (e.g., exposing or targeting port 8000 when the application was listening on port 80, or vice-versa).
**Resolution:** Aligned the ports across the `Deployment`, `Service`, `Ingress`, and `NetworkPolicy` manifests to ensure traffic flows correctly from the load balancer down to the container port.

### 2. Pydantic Private Attribute Bugs
**Symptoms:** Validation errors or unexpected serialization behavior when instantiating or dumping state objects.
**Root Cause:** Improper usage of private attributes in Pydantic v2 models. Standard attributes starting with `_` were either ignored or caused validation failures depending on the context.
**Resolution:** Updated the Pydantic models to correctly use `PrivateAttr` from the `pydantic` package for any internal state variables, ensuring they are excluded from schema validation and serialization but remain accessible in the code.

### 3. Anthropic Model Slug Corrections
**Symptoms:** API calls to Anthropic failed with "Model not found" or similar validation errors.
**Root Cause:** Incorrect or outdated model slugs were being used in the LangGraph orchestration configuration (e.g., using an invalid version identifier for Claude 3.5 Sonnet).
**Resolution:** Updated the model slugs to the correct supported versions (e.g., `claude-3-5-sonnet-20241022`) in the runtime configuration and model router logic.

### 4. DigitalOcean Valkey Secret Injection
**Symptoms:** The orchestrator failed to connect to the Valkey (Redis alternative) instance for checkpointing and state management.
**Root Cause:** The connection credentials were not properly propagated into the Kubernetes environment.
**Resolution:** Used `doctl` to fetch the managed Valkey connection string and injected it into a Kubernetes `Secret` in the `lula-orch` namespace. Configured the orchestrator pods to consume this secret as an environment variable (`REDIS_URL` or `VALKEY_URL`).

## 2026-03-28: Cloudflare 522 — REGIONAL_NETWORK NLB Failure

### Symptom
Cloudflare 522 (Connection Timed Out) on `lula.eiv.eng.br`. Direct curl to origin IP `45.55.125.230` also timed out.

### Root Cause
The ingress-nginx-controller service was provisioned as a `REGIONAL_NETWORK` type DO Load Balancer. This NLB type:
1. Required forwarding rules to target NodePorts (30900/32134), not ports 80/443 directly
2. Required the specific node running the ingress pod to be in the backend pool (due to `externalTrafficPolicy: Local`)
3. Had a broken health check targeting a stale NodePort (30658)
4. Even after fixing all forwarding rules, the NLB VIP remained unresponsive

### Fix
1. Deleted the broken `REGIONAL_NETWORK` NLB and the ingress-nginx-controller service
2. Recreated the service as a standard DO Load Balancer (no `service.beta.kubernetes.io/do-loadbalancer-type` annotation)
3. New LB provisioned at IP `159.203.146.12`
4. Updated DO Cloud Firewalls to allow NodePorts 30658, 30900, 32134 from `0.0.0.0/0`
5. Updated Cloudflare DNS A record from `45.55.125.230` to `159.203.146.12`

### Prevention
Do not use `REGIONAL_NETWORK` NLB type for ingress-nginx on DOKS unless you fully understand the NodePort forwarding requirements. The standard DO Load Balancer (no type annotation) works correctly with ingress-nginx out of the box.

### New LB manifest
See [`infra/k8s/ingress-nginx-svc.yaml`](infra/k8s/ingress-nginx-svc.yaml) for the correct service definition.

---

## 2026-03-28: SPA Tasks Stuck in "running" — Redis/Valkey Unreachable

### Symptom
Tasks submitted via the SPA remained in `status: "running"` indefinitely with `log_lines: 0` and `finished_at: null`. The SSE stream kept emitting identical "running" events with no progress.

### Root Cause
The managed DigitalOcean Valkey (Redis-compatible) instance at `rediss://...lula-valkey-do-user-...ondigitalocean.com:25061` was unreachable from the DOKS cluster. The `RedisCheckpointSaver` in [`py/src/lg_orch/backends/redis.py`](../py/src/lg_orch/backends/redis.py) created `redis.from_url()` clients **without** `socket_connect_timeout` or `socket_timeout` parameters. When the graph subprocess (`python -m lg_orch.main run ...`) attempted to create the Redis checkpoint saver and perform the first `get_tuple` call, the TCP connect blocked indefinitely with no timeout, producing zero stdout output.

The `_capture_process_output` thread in [`py/src/lg_orch/api/service.py`](../py/src/lg_orch/api/service.py) reads stdout line-by-line; since the subprocess never wrote anything, `log_lines` stayed at 0 and `finished_at` was never set.

### Fix
1. **Added `socket_connect_timeout` (5s) and `socket_timeout` (10s)** to both sync and async Redis clients in `RedisCheckpointSaver.__init__` so connections fail fast instead of hanging
2. **Added a Redis health-check with SQLite fallback** in [`py/src/lg_orch/commands/run.py`](../py/src/lg_orch/commands/run.py): after creating the Redis saver, a `ping()` is issued; if it fails (timeout or connection error), the backend silently falls back to SQLite checkpointing with a warning log
3. **Updated the DO model API key** in the `lula-secrets` Kubernetes secret (`DIGITAL_OCEAN_MODEL_ACCESS_KEY`)

### Prevention
- Always set `socket_connect_timeout` and `socket_timeout` on Redis clients to prevent indefinite hangs
- External managed databases (Valkey, Postgres) should have connectivity health-checks at startup with graceful fallback
- The subprocess spawned by the remote API should emit a startup banner line to stdout before any blocking I/O, so `log_lines: 0` after N seconds can be detected as a hang

### Verification
```bash
# After deploying the fix, submit a test task:
curl -X POST https://lula.eiv.eng.br/v1/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"request":"write a python script that prints hello"}'

# The run should either complete or show log_lines > 0 within 10 seconds.
# If Redis is down, logs will show: checkpoint_redis_unreachable fallback=sqlite
```

---

## Maintenance Notes
- Temporary diagnostic files (traces, local dumps, patched manifests) were cleaned up from the repository root to maintain a pristine environment.
- The deployment process has been streamlined to build the `lula-orch` image, push it to the DigitalOcean Container Registry, and trigger a rolling restart of the deployment to ensure zero-downtime updates.
