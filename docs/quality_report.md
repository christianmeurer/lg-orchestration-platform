# Lula — Code Quality and Maturity Report

**Version:** v0.7 (Alpha → Beta transition)
**Report Date:** 2026-03-20
**Scope:** Full codebase analysis covering Python orchestration, Rust runner, Kubernetes infrastructure, GitOps, eval framework, CI/CD, runtime configuration, JSON schemas, and documentation.

---

## 1. Executive Summary

Lula occupies a differentiated position in the open-source agentic coding landscape. The architecture is significantly ahead of field peers in enterprise-grade capabilities: governed approval flow, tripartite persistent memory, multi-agent DAG scheduling, checkpoint-based suspend/resume, bidirectional audit trails, and a strict reasoning/execution split enforced at a process boundary. These properties are architectural commitments, not incremental features.

**Overall maturity: Alpha → Beta.** The system is capable and coherent but has not yet closed the gap between its architectural specification and runtime enforcement across all layers.

| Sub-system | Maturity |
|---|---|
| Rust runner | Beta — strongest sub-system; hardened, tested, correct |
| Python orchestration core | Beta — correct and well-tested; type enforcement gap at graph boundary |
| Infrastructure layer | Alpha — manifests are correct in intent but had namespace and securityContext gaps (addressed in v0.7) |

---

## 2. Component Maturity Matrix

| Component | Maturity | Top Blocker |
|---|---|---|
| Python orchestration ([`py/src/lg_orch/`](py/src/lg_orch/)) | Beta | `StateGraph(dict)` — runtime state is untyped at the graph boundary; Pydantic schema documents intent but is not enforced |
| Rust runner ([`rs/runner/src/`](rs/runner/src/)) | Beta | MCP subprocess connection pooling absent; each call spawns a new subprocess |
| Kubernetes infra ([`infra/k8s/`](infra/k8s/)) | Alpha → Beta | NetworkPolicy namespace mismatch and orchestrator securityContext gaps addressed in v0.7; image tag pinning and automated registry pipeline remain open |
| GitOps / ArgoCD ([`infra/k8s/argocd-app.yaml`](infra/k8s/argocd-app.yaml)) | Alpha | No automated CI → registry → ArgoCD sync pipeline; deployment is manual |
| Eval framework ([`eval/`](eval/)) | Alpha | Golden file assertions (`post_apply_pytest_pass`) are not executed against runner output; eval run is structural scoring only until Wave 11 |
| CI/CD ([`.github/workflows/`](.github/workflows/)) | Alpha | Eval correctness job exists but does not gate on pass rate; no automated image push or staging deploy |
| Runtime config ([`configs/`](configs/)) | Alpha | `redis_url` hardcoded in stage config; stage backend uses SQLite instead of Redis |
| JSON schemas ([`schemas/`](schemas/)) | Beta | Schemas are correct and well-specified; not enforced at runtime boundaries |
| Documentation ([`docs/`](docs/)) | Beta | Architecture docs are comprehensive; deployment guide has minor gaps relative to current manifest state |

---

## 3. Architecture Strengths

The following seven capabilities are differentiated relative to the current open-source agentic coding field (Aider, OpenHands, SWE-agent, Plandex, Goose):

1. **Multi-class failure classification and SLA-aware routing.** [`model_routing.py`](py/src/lg_orch/model_routing.py) routes tasks by failure class (syntax, logic, flaky, timeout, resource) to lane-specific model configurations with cost, latency, and quality SLA targets. No peer implements this.

2. **Governed approval flow with durable audit trail.** [`approval_policy.py`](py/src/lg_orch/approval_policy.py) implements `TimedApprovalPolicy`, `QuorumApprovalPolicy`, and `RoleApprovalPolicy` with HMAC-SHA256 token gating, TTL enforcement, and key rotation. Approval actions are surfaced in the REST API, SSE SPA, and VS Code extension. No peer implements multi-path approval governance.

3. **Checkpoint-based suspend/resume.** [`checkpointing.py`](py/src/lg_orch/checkpointing.py) links LangGraph SQLite checkpoints to git snapshot identifiers so a suspended run can be restored to exact filesystem and graph state. This is unique among open-source agentic coding tools.

4. **Tripartite persistent memory without external dependencies.** [`memory.py`](py/src/lg_orch/memory.py), [`long_term_memory.py`](py/src/lg_orch/long_term_memory.py), and [`procedure_cache.py`](py/src/lg_orch/procedure_cache.py) implement semantic (cosine similarity), episodic (cross-session recovery facts), and procedural (verified tool sequences) memory tiers in SQLite. No external vector database is required.

5. **Immutable audit trail with structured provenance.** [`audit.py`](py/src/lg_orch/audit.py) records every tool call, approval action, and graph transition with structured metadata. [`trace.py`](py/src/lg_orch/trace.py) carries provenance across agent handoffs via typed `AgentHandoff` envelopes.

6. **MetaGraph multi-agent DAG scheduler with git worktree isolation.** [`meta_graph.py`](py/src/lg_orch/meta_graph.py) implements Kahn topological sort, cycle detection, dynamic rewiring, and per-agent git worktree branch isolation. [`multi_repo.py`](py/src/lg_orch/multi_repo.py) adds SCIP-based cross-repo dependency ordering.

7. **Multi-repo orchestration with SCIP cross-repo symbol indexing.** [`scip_index.py`](py/src/lg_orch/scip_index.py) reads SCIP index artifacts to resolve cross-repository symbol definitions. `MultiRepoScheduler` uses this to inject dependency-ordered sub-agent handoffs. No peer implements cross-repo symbol-aware scheduling.

---

## 4. Critical Findings (Addressed in v0.7)

The following issues were identified in the analysis and addressed in this commit.

### 4.1 MCP Command Sandboxing Gap

**File:** [`rs/runner/src/tools/mcp.rs`](rs/runner/src/tools/mcp.rs)

MCP tool calls invoked subprocess commands without routing through the runner's allowlist and injection detection pipeline. Commands sourced from MCP server responses could bypass the invariant checker. Fixed by wiring MCP dispatch through the same allowlist and injection scan applied to `exec` tool calls.

### 4.2 NetworkPolicy Namespace Mismatch

**File:** [`infra/k8s/network-policy.yaml`](infra/k8s/network-policy.yaml)

The `podSelector` and `namespaceSelector` labels in the egress rules did not match the namespace labels declared in [`infra/k8s/namespace.yaml`](infra/k8s/namespace.yaml). The policy was syntactically valid but would not select the intended pods at runtime, effectively leaving the network policy unenforced. Label selectors aligned in v0.7.

### 4.3 Orchestrator securityContext Missing

**File:** [`infra/k8s/deployment.yaml`](infra/k8s/deployment.yaml)

The orchestrator deployment lacked a `securityContext` at both pod and container level. The runner deployment was hardened (`readOnlyRootFilesystem`, `CAP_DROP ALL`, `allowPrivilegeEscalation: false`, `seccompProfile: RuntimeDefault`) but the orchestrator ran with default (permissive) security settings. Fixed by adding equivalent `securityContext` to the orchestrator pod spec.

### 4.4 Runner Service LoadBalancer → ClusterIP

**File:** [`infra/k8s/runner-service.yaml`](infra/k8s/runner-service.yaml)

The runner `Service` was typed `LoadBalancer`, which would provision a cloud load balancer and expose the runner endpoint externally. The runner is an internal service that must only be reachable from the orchestrator pod. Changed to `ClusterIP`. Access from outside the cluster must go through the orchestrator API.

### 4.5 JWKS Cache TTL Absent

**File:** [`py/src/lg_orch/auth.py`](py/src/lg_orch/auth.py)

The JWKS key cache has no TTL or expiry. If a signing key is rotated at the identity provider, the old key remains cached in-process until restart, either accepting tokens signed by rotated keys or rejecting valid tokens depending on the rotation strategy. A TTL and periodic refresh interval was added.

### 4.6 SqliteCheckpointSaver Event-Loop Blocking

**File:** [`py/src/lg_orch/checkpointing.py`](py/src/lg_orch/checkpointing.py)

`SqliteCheckpointSaver` performed synchronous SQLite reads and writes on the async event loop. Under concurrent run load this would stall all coroutines for the duration of each checkpoint write. Fixed by wrapping checkpoint I/O in `asyncio.to_thread`.

### 4.7 Runtime Config: redis_url Hardcode and Stage Backend

**Files:** [`configs/runtime.stage.toml`](configs/runtime.stage.toml), [`configs/runtime.dev.toml`](configs/runtime.dev.toml)

The stage config contained a hardcoded `redis_url` pointing to a local address rather than reading from an environment variable. The stage checkpoint backend was set to `sqlite` rather than `redis`, negating the purpose of the stage environment as a production proxy. Both corrected: `redis_url` now reads from `LG_REDIS_URL` and stage backend set to `redis`.

---

## 5. Remaining Technical Debt

The following items were identified in the v0.7 quality audit and are tracked for resolution in upcoming waves. Items addressed in this commit are documented in [Critical Findings (Addressed in v0.7)](#4-critical-findings-addressed-in-v07).

### Wave 14 — High Priority

#### Typed Graph State Migration
- **Files**: [`py/src/lg_orch/graph.py`](../py/src/lg_orch/graph.py), [`py/src/lg_orch/state.py`](../py/src/lg_orch/state.py)
- **Issue**: `StateGraph(dict)` passes untyped `dict[str, Any]` at runtime. `OrchState` (Pydantic v2, `extra="forbid"`) documents the intended schema but nodes never validate against it. Key typos produce silent `None` rather than a validation error.
- **Fix**: Migrate to `StateGraph(OrchState)` with `Annotated[T, operator.add]` reducers for list fields. Validate inbound state against `OrchState.model_validate()` in the `ingest` node.
- **Test impact**: All 62 test files that construct state as raw dicts will require `OrchState(**...)` construction.

#### Eval Golden File Enforcement
- **Files**: [`eval/run.py`](../eval/run.py), [`eval/golden/`](../eval/golden/)
- **Issue**: `score_task()` in `run.py` does not load or assert any golden file. The golden JSON assertion system (`eq`, `lte`, `gte`, `in`, `contains`) is documentation only. The nightly CI gate cannot detect outcome-correctness regressions.
- **Fix**: In `score_task()`, load the corresponding `eval/golden/{task_id}.json` if it exists and evaluate each assertion against `result`. Count assertion failures as additional score deductions. Update `test_eval_correctness.py` to cover the assertion evaluation paths.
- **Dependency**: Wave 11 runner integration (fixture task execution) is a prerequisite for full correctness testing.

#### Automated CI → Registry → Deploy Pipeline
- **Files**: [`.github/workflows/`](../.github/workflows/)
- **Issue**: CI has no Docker build, push, or CVE scan step. Every production deployment is a manual `do_deploy.sh` invocation. No link between green CI and a deployed image.
- **Fix**: Add a `release.yml` workflow triggered on `v*` tag push: build multi-arch image, push to DOCR with SHA tag, run `trivy` scan, update `deployment.yaml` image ref via `kustomize edit set image`, open a PR or auto-merge. CI `eval-canary` job should gate the release workflow.

#### Image Digest Pinning
- **Files**: [`infra/k8s/deployment.yaml`](../infra/k8s/deployment.yaml), [`infra/k8s/runner-deployment.yaml`](../infra/k8s/runner-deployment.yaml)
- **Issue**: All K8s manifests use `:latest`. Any registry push silently changes the running image at next pod restart.
- **Fix**: Replace `:latest` with `@sha256:<digest>` or semver tags. Use ArgoCD Image Updater to automate tag promotion from registry to Git.

### Wave 15 — Medium Priority

#### `planner.py` Decomposition
- **File**: [`py/src/lg_orch/nodes/planner.py`](../py/src/lg_orch/nodes/planner.py) (835 lines)
- **Issue**: Single file mixes intent classification (`_classify_intent`), LLM prompt construction, semantic/procedural memory ranking, MCP catalog injection, JSON schema validation, recovery packet merging, and fallback plan generation. Testing any one concern requires loading all concerns.
- **Fix**: Decompose into:
  - `py/src/lg_orch/nodes/_planner_prompt.py` — prompt construction and LLM call
  - `py/src/lg_orch/nodes/_planner_memory.py` — semantic/procedural memory ranking
  - `py/src/lg_orch/nodes/planner.py` — thin orchestrator importing the above

#### Firecracker API Integration
- **Files**: [`rs/runner/src/tools/exec.rs`](../rs/runner/src/tools/exec.rs), [`rs/runner/src/sandbox.rs`](../rs/runner/src/sandbox.rs)
- **Issue**: `MicroVmEphemeral` backend invokes the `firecracker` binary via CLI flags. This is not the Firecracker API (which uses a Unix domain socket with a REST JSON API for VM configuration). All current deployments degrade to `LinuxNamespace` or `SafeFallback`.
- **Fix**: Implement the Firecracker VMM API: create tap interface, PUT `/machine-config`, PUT `/drives/rootfs`, PUT `/network-interfaces/eth0`, PUT `/actions` (`InstanceStart`). Use `tokio::net::UnixStream` + `hyper` for socket-level HTTP.

#### MCP Subprocess Connection Pooling
- **File**: [`rs/runner/src/tools/mcp.rs`](../rs/runner/src/tools/mcp.rs)
- **Issue**: Every `mcp_discover` / `mcp_execute` call spawns a new subprocess, performs the full MCP handshake, makes one request, and drops the client. For JVM-backed or large-script MCP servers, startup overhead is ≥100ms per call.
- **Fix**: Implement a `HashMap<String, McpStdioClient>` pool keyed by `server.command`. Reuse live connections; restart on I/O error. Apply a per-server connection TTL (e.g., 5 minutes) to prevent indefinite zombie processes.

#### Secrets Management
- **Files**: [`infra/k8s/`](../infra/k8s/), `scripts/argocd_bootstrap.sh`
- **Issue**: Secrets are managed via manual `kubectl edit secret`. No External Secrets Operator, Vault, or SOPS integration. Secrets cannot be version-controlled, rotated automatically, or audited.
- **Fix**: Adopt one of: (a) External Secrets Operator + AWS/GCP/DO Secrets Manager, (b) SOPS-encrypted secrets committed to Git with ArgoCD SOPS plugin, or (c) Vault Agent Injector. At minimum, add `sealed-secrets` to the ArgoCD bootstrap.

### Wave 16 — Low Priority

#### `_sla_policy` Module-Level Global Refactor
- **File**: [`py/src/lg_orch/tools/inference_client.py`](../py/src/lg_orch/tools/inference_client.py)
- **Issue**: `_sla_policy` is a mutable module-level `SlaRoutingPolicy` singleton. Unsafe for multi-tenant deployments (cross-tenant latency contamination) and breaks test isolation (state leaks between test cases).
- **Fix**: Pass `SlaRoutingPolicy` as a constructor parameter to `InferenceClient`, or store it in a `contextvars.ContextVar` for per-request isolation.

#### Healing Loop Multi-Runner Support
- **File**: [`py/src/lg_orch/healing_loop.py`](../py/src/lg_orch/healing_loop.py)
- **Issue**: Subprocess call is hardcoded to `python -m pytest`. No support for `cargo test`, `npm test`, `go test`, `jest`, or other runners.
- **Fix**: Add project-type detection (presence of `Cargo.toml`, `package.json`, `pyproject.toml`, `go.mod`) and dispatch to the appropriate test runner. Make the runner command configurable via `AppConfig`.

#### ArgoCD Image Updater + Sync Windows
- **File**: [`infra/k8s/argocd-app.yaml`](../infra/k8s/argocd-app.yaml)
- **Issue**: Every push to `main` deploys immediately to production with no gate. No change-freeze windows. No automated image tag promotion.
- **Fix**: Install ArgoCD Image Updater with a `semver` update strategy. Add sync windows to `argocd-app.yaml` (e.g., allow only 09:00–17:00 UTC on weekdays for production). Use tag-based promotion (not branch-based).

#### `DefaultHasher` Instability in Indexing
- **File**: [`rs/runner/src/indexing.rs`](../rs/runner/src/indexing.rs)
- **Issue**: `DefaultHasher` is used to derive the SQLite index path from the root directory. `DefaultHasher` output is not stable across Rust versions or minor releases. After a compiler upgrade, the index path changes and the old index is orphaned.
- **Fix**: Replace `DefaultHasher` with `fnv::FnvHasher` (already a transitive dependency) or compute the path hash using `sha2` (already present as a dependency).

---

## 6. Market Comparison

Lula compared against the five closest open-source peers across fifteen enterprise-relevant capabilities.

| Capability | Lula | Aider | OpenHands | SWE-agent | Plandex | Goose |
|---|---|---|---|---|---|---|
| Multi-class failure taxonomy routing | Yes | No | No | No | No | No |
| SLA-aware model routing (cost/latency/quality lanes) | Yes | No | No | No | No | No |
| Human-in-the-loop approval gating (multi-path) | Yes | No | Partial | No | No | No |
| HMAC-signed approval tokens with TTL | Yes | No | No | No | No | No |
| Checkpoint-based suspend/resume | Yes | No | No | No | Partial | No |
| Tripartite persistent memory (no external DB) | Yes | No | No | No | No | No |
| Immutable structured audit trail | Yes | No | Partial | No | No | No |
| MetaGraph multi-agent DAG scheduling | Yes | No | No | No | No | No |
| Git worktree branch isolation per agent | Yes | No | No | No | No | No |
| Multi-repo SCIP cross-repo symbol indexing | Yes | No | No | No | No | No |
| Rust sandboxed execution runner | Yes | No | No | No | No | No |
| gVisor / Kata runtimeClass on K8s | Yes | No | No | No | No | No |
| MCP 2024-11-05 full protocol with schema-hash pinning | Yes | No | No | No | No | No |
| Prompt injection detection (Unicode, RCE, mining) | Yes | No | Partial | No | No | No |
| Full eval framework with golden-file assertions | Partial | No | No | Partial | No | No |

---

## 7. Recommended Remediation Roadmap

### Immediate (current sprint, Wave 10)

| Action | File(s) |
|---|---|
| Add TTL-based JWKS cache refresh (completed in v0.7) | [`py/src/lg_orch/auth.py`](py/src/lg_orch/auth.py) |
| Fix SqliteCheckpointSaver blocking I/O (completed in v0.7) | [`py/src/lg_orch/checkpointing.py`](py/src/lg_orch/checkpointing.py) |
| Fix NetworkPolicy selectors (completed in v0.7) | [`infra/k8s/network-policy.yaml`](infra/k8s/network-policy.yaml) |
| Add orchestrator securityContext (completed in v0.7) | [`infra/k8s/deployment.yaml`](infra/k8s/deployment.yaml) |
| Change runner service to ClusterIP (completed in v0.7) | [`infra/k8s/runner-service.yaml`](infra/k8s/runner-service.yaml) |
| Fix redis_url and stage backend config (completed in v0.7) | [`configs/runtime.stage.toml`](configs/runtime.stage.toml) |
| Pin image tags to digests in K8s manifests | [`infra/k8s/deployment.yaml`](infra/k8s/deployment.yaml), [`infra/k8s/runner-deployment.yaml`](infra/k8s/runner-deployment.yaml) |
| Implement CI → registry → ArgoCD sync pipeline | [`.github/workflows/`](.github/workflows/) |
| Decompose [`planner.py`](py/src/lg_orch/nodes/planner.py) into sub-modules | [`py/src/lg_orch/nodes/planner.py`](py/src/lg_orch/nodes/planner.py) |

### Short-Term (Wave 11 — Eval correctness)

| Action | File(s) |
|---|---|
| Wire eval runner to execute `post_apply_pytest_pass` assertions against runner output | [`eval/run.py`](eval/run.py) |
| Implement pass@k scoring and SWE-bench lite adapter | [`eval/run.py`](eval/run.py) |
| Gate CI eval job on pass rate threshold | [`.github/workflows/eval-correctness.yml`](.github/workflows/eval-correctness.yml) |
| Add multi-language test runner support to healing loop (Jest, Cargo test, Go test) | [`py/src/lg_orch/healing_loop.py`](py/src/lg_orch/healing_loop.py) |

### Medium-Term (Wave 13 — Schema hardening and sandbox completion)

| Action | File(s) |
|---|---|
| Migrate `StateGraph(dict)` to `StateGraph(OrchState)` to enforce Pydantic schema at graph boundary | [`py/src/lg_orch/graph.py`](py/src/lg_orch/graph.py), [`py/src/lg_orch/state.py`](py/src/lg_orch/state.py) |
| Wire Firecracker HTTP API client in sandbox dispatch | [`rs/runner/src/sandbox.rs`](rs/runner/src/sandbox.rs) |
| Implement MCP subprocess connection pool | [`rs/runner/src/tools/mcp.rs`](rs/runner/src/tools/mcp.rs) |
| Enforce JSON schema validation at tool envelope boundaries at runtime | [`schemas/tool_envelope.schema.json`](schemas/tool_envelope.schema.json), [`rs/runner/src/envelope.rs`](rs/runner/src/envelope.rs) |
