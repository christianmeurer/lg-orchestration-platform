# Lula — Roadmap

_Derived from `docs/quality_report.md` (2026-03-20). Items are ordered by severity and then by layer._

## Wave 1 — Critical Python Fixes ✅
- [x] Fix `build_meta_graph()` import crash in `main.py`
- [x] Restrict auth open-fallback: vote/approval-policy endpoints require authentication
- [x] Replace silent `except Exception: pass` in `audit.py` S3/GCS export with structured error logging

## Wave 2 — Critical Rust Fixes ✅
- [x] Add graceful HTTP shutdown (`ctrl_c` signal) to `rs/runner/src/main.rs`
- [x] Cap MCP `Content-Length` allocation at 64 MiB in `rs/runner/src/tools/mcp.rs`
- [x] Change default sandbox preference to `LinuxNamespace` when `unshare` is available

## Wave 3 — Infrastructure / DevOps Fixes ✅
- [x] Fix `Dockerfile.python`: non-root user, replace `curl | sh` with pinned installer
- [x] Restrict `sourceRepos` in `infra/k8s/argocd-project.yaml` to exact repo URL
- [x] Remove RBAC self-management from ArgoCD `ClusterRole` in `infra/k8s/argocd-rbac.yaml`
- [x] Add `NetworkPolicy` for `lula-orch` with explicit egress allowlist
- [x] Pin `trivy-action` to commit SHA in CI workflows
- [x] Add cosign image signing step to release workflow

## Wave 4 — Rust Soundness Fixes ✅
- [x] Replace `AF_VSOCK`-as-`TcpStream` with `AsyncFd<OwnedFd>` in `rs/runner/src/vsock.rs`
- [x] Replace same pattern in `rs/guest-agent/src/main.rs`
- [x] Add per-repo mutex for concurrent `git reset --hard` in `rs/runner/src/snapshots.rs`
- [x] Add guest agent `cmd` allowlist check

## Wave 5 — Python Long-Term Memory ✅
- [x] Wire real embedding provider (configurable, with Ollama/OpenAI adapters)
- [x] Add startup warning when `stub_embedder` is active
- [x] Document O(n) scan limitation; add row-count guard (warn at > 5 000 rows)

## Wave 6 — Structural Debt ✅
- [x] Split `py/src/lg_orch/checkpointing.py` into `backends/` submodule
- [x] Refactor `_api_http_dispatch()` in `remote_api.py` to dispatch table
- [x] Migrate `worktree.py` from stdlib `logging` to `structlog`
- [x] Migrate `python-jose` to `PyJWT`
- [x] Fix `model_routing.py` dict surgery with Pydantic model

## Wave 7 — Test & Eval Completion ✅
- [x] Add `--cov-fail-under=80` gate to CI
- [x] Remove `LG_E2E=1` guard from structural smoke tests in `test_e2e.py`
- [x] Complete golden assertion files for all 8 eval task categories
- [x] Parallelize `pass@k` multi-run loop in `eval/run.py`

## Wave 8 — Security & Correctness (2026-03-28) ✅
- [x] Snapshot ID validation against `^[a-zA-Z0-9_-]{1,64}$` before git ref construction (`snapshots.rs`)
- [x] MCP server env key allowlist — blocks `LD_*`, `DYLD_*`, `*PRELOAD*`, `SHELL`, `IFS`, `BASH_ENV` (`tools/mcp.rs`)
- [x] Internal error details no longer leaked to HTTP clients — full error logged server-side, generic string returned (`errors.rs`)
- [x] Approval secret cached in `OnceLock` — eliminates per-request env var read under lock (`approval.rs`)
- [x] OTel trace context stored in request extensions instead of dropped guard (`auth.rs`)

## Wave 9 — Performance & Hygiene (2026-03-28) ✅
- [x] Diagnostics regex compiled once via `LazyLock` statics instead of per-call (`diagnostics.rs`)
- [x] Release profile: `lto = "thin"`, `codegen-units = 1`, `opt-level = 3`, `strip = "symbols"` (`Cargo.toml`)
- [x] License aligned to MIT across all workspace crates (`Cargo.toml`)
- [x] Guest agent vsock listener binds to `VMADDR_CID_HOST` (2) instead of `VMADDR_CID_ANY` (`guest-agent/src/main.rs`)
- [x] `timing_ms` changed from `u128` to `u64` — prevents JavaScript JSON precision loss (`envelope.rs`)

## Wave 10 — Python Orchestrator Fixes (2026-03-28) ✅
- [x] Local-model path passes `VerifierReport` to `_default_plan()` — recovery steps included in default plan (`planner.py`, `_planner_prompt.py`)
- [x] `SlaRoutingPolicy.select_model()` wired into `_planner_model_output()` — SLA-aware model selection active (`planner.py`)
- [x] `cleanup_orphaned_worktrees()` added — scans and removes orphaned `lg-orch/` git worktrees on startup (`worktree.py`)
- [x] `HealingLoop` typed handoff — structured `healing_context` dict instead of formatted string; post-healing verification check (`healing_loop.py`)

## Wave 11 — Backlog Items (2026-03-28) ✅
- [x] `OllamaEmbedder` + `make_embedder()` factory — configurable embedding provider via `LG_EMBED_PROVIDER` env var (`long_term_memory.py`)
- [x] `startupProbe` added to runner and orchestrator K8s deployments and Helm chart templates
- [x] `ScipIndex.mark_stale()` + `is_stale` property — index invalidated after `apply_patch` operations (`scip_index.py`, `executor.py`)
- [x] Batch size limit: `MAX_BATCH_SIZE = 50` in `batch_execute_tool` (`main.rs`)
- [x] Maximum timeout cap: `MAX_TIMEOUT_SECS = 3600` in exec tool (`exec.rs`)

## Deployment Fixes (2026-03-28) ✅
- [x] `--root-dir /workspace` (was `/app`) — commands now run in writable emptyDir volume
- [x] `HOME`, `TMPDIR`, `XDG_CACHE_HOME` env vars with `/workspace` fallbacks in exec tool and deployment manifest
- [x] Prod write allowlist changed from empty to `[".", "**"]` — `apply_patch` now works in prod
- [x] Default `_runner_base_url` reads from `LG_RUNNER_BASE_URL` env var with K8s DNS fallback
- [x] `automountServiceAccountToken: false` added to runner pod spec
- [x] Batch executor returns partial results — single tool failure no longer aborts entire batch
- [x] Startup cgroup v2 probe emits Prometheus metric `runner_cgroup_available`

## Phase 2 Audit — Python LOW Fixes (2026-03-29) ✅
- [x] `graph.py`: OTel double-call bug fixed — node function called exactly once; exceptions recorded on span with StatusCode.ERROR
- [x] `vericoding.py`: Space removed from `_SHELL_METACHARS` — `create_subprocess_exec` does not use a shell, spaces in args are safe

## Phase 3 — Rust Codebase Audit (2026-03-29) ✅
- [x] Full audit of `fs.rs`, `mod.rs`, `exec.rs`, `indexing.rs`, `invariants.rs` — no new issues found
- [x] Clippy clean: zero warnings with `-D warnings`
- [x] All blocking I/O properly handled (spawn_blocking or dedicated std::thread)

## Phase 4 — Helm/K8s Fixes (2026-03-29) ✅
- [x] Helm `runner-deployment.yaml`: `runtimeClassName` and `nodeSelector` conditional on `.Values.runner.gvisor.enabled`
- [x] Helm `values.yaml`: Added `runner.gvisor.enabled: true` with documentation comment
- [x] `secrets.yaml.example`: Added `LG_RUNNER_APPROVAL_SECRET` to example

## Phase 5 — ROADMAP Verification (2026-03-29) ✅
- [x] `approval.rs` OnceLock — already completed in Wave 8
- [x] `config.rs` prod allowlist — documented; root_dir=/workspace makes `[".", "**"]` correct
- [x] `startupProbe` — present in all four deployment manifests

## Wave 13 — 9.5/10 Feature Set (2026-03-29) ✅

- [x] TOCTOU path traversal fixed with cap-std confinement (rs/runner/src/tools/fs.rs, invariants.rs)
- [x] OllamaEmbedder wired as default embedding provider (LG_EMBED_PROVIDER env var)
- [x] PVC-backed persistent workspace option (charts/lula/templates/workspace-pvc.yaml)
- [x] Real-time tool stdout streaming via SSE (tool_stdout events in streaming.py)
- [x] Resume/approval UI in SPA for suspended runs
- [x] VS Code extension implemented (lula.runTask, lula.showRuns, lula.configure)

## Wave 14 — Closing the Final 0.5 (2026-03-30) ✅

- [x] Ollama deployed as sidecar — `nomic-embed-text` model pulled at init, `LG_EMBED_PROVIDER=ollama` set in production
- [x] Firecracker Tier 3 node scheduling — `runner.firecracker.enabled` Helm value with KVM nodeSelector/tolerations, `/dev/kvm` device mount, env var activation
- [x] VS Code extension packaged — VSIX built, marketplace metadata complete, CI/CD workflow for automated publishing (`vscode-publish.yml`)
- [x] Helm chart updated — Ollama sidecar container, init container for model pull, conditional Firecracker volumes/env

## Research-Driven Optimizations (2026-04-01) ✅
- [x] sqlite-vec vector index replaces O(n) numpy cosine scan in `long_term_memory.py` — indexed search with transparent numpy fallback
- [x] SYMPHONY-inspired `DiversityRoutingPolicy` — round-robin heterogeneous model selection via `LG_MODEL_DIVERSITY=true` env var (`model_routing.py`)

## Backlog (Medium-term) — Completed
- [x] Implement External Secrets Operator integration for K8s secret management — manifests at infra/k8s/external-secrets/
- [x] Add SBOM generation (CycloneDX) to release workflow — anchore/sbom-action in release.yml
- [x] VS Code extension published — vscode-publish.yml workflow, VSIX built, marketplace metadata complete

## Wave 15 — Product Polish (2026-04-01) ✅

- [x] Leptos WASM SPA replacing 3 legacy frontends — Cyberpunk Minimal design, SSE streaming, approval modals, 4 pages (`rs/spa-leptos/`)
- [x] VS Code extension rich operations console — webview with live SSE, approval workflow, diff preview, esbuild build (`vscode-extension/`)
- [x] Rich CLI with `rich` library — panels, tables, colored markup, stderr log separation (`console.py`, `visualize.py`)
- [x] CI pipeline fully green — nightly rustfmt, ruff/mypy clean, eval JSON fix
- [x] Codebase cleanup — stale artifacts removed, mixed logging fixed, dead `heal` command wired
- [x] DiversityRoutingPolicy wired into planner via `get_routing_policy()` factory
- [x] 1042 tests, 78% coverage, gate enforced at 78% in CI and pyproject.toml
- [x] Comprehensive documentation overhaul — README, architecture, CONTRIBUTING, SECURITY, quality report
- [x] SBOM generation (CycloneDX) in release workflow
- [x] External Secrets Operator manifests at `infra/k8s/external-secrets/`
- [x] VS Code extension publish workflow (`vscode-publish.yml`)

## Wave 16 — Full Stack Completion (2026-04-02) ✅

### Product & UX
- [x] Light/dark mode toggle for Leptos SPA with `localStorage` persistence (`global.css`, `settings.rs`)
- [x] VS Code extension: inject active file/selection context into task submission (`extension.ts`)
- [x] Leptos SPA: resizable split panels with drag handle (`split_pane.rs`, `dashboard.rs`, `run_detail.rs`)
- [x] Leptos SPA: keyboard shortcuts — Ctrl+Enter submit, Escape dismiss modal (`console.rs`)

### Architecture & Performance
- [x] 1,463 tests, 81% coverage, gate ratcheted to 81% in CI and pyproject.toml
- [x] E2E integration smoke tests for SPA/API contract (`test_e2e_spa.py`)
- [x] Pinned Trunk v0.21.14 in CI and Dockerfile for reproducible WASM builds

### Infrastructure & Operations
- [x] GitHub Actions opted into Node.js 24 (`FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true`)
- [x] Grafana dashboard template for Prometheus metrics (`infra/monitoring/grafana-dashboard.json`)
- [x] LLM egress NetworkPolicy documented with provider breakdown (`network-policy.yaml`)
- [x] DOKS deployment: cluster `lula-prod` in nyc3, image pushed, all pods running

### Research-Aligned
- [x] GLEAN verification framework — guideline-grounded agent action auditing (`glean.py`, 11 tests)
- [x] Temperature diversity mixin for pluralistic alignment (`model_routing.py`)

## Wave 17 — Production Deployment (2026-04-02) ✅

### Infrastructure
- [x] nginx ingress controller installed on DOKS with cert-manager + Let's Encrypt ClusterIssuer
- [x] DNS A record `lula.eiv.eng.br → 134.199.245.159` (pending NS delegation at registrar)
- [x] Service switched from LoadBalancer to ClusterIP (ingress handles routing)
- [x] Helm chart published to OCI registry: `oci://registry.digitalocean.com/lula-orch/lula:1.1.0`

### Architecture
- [x] 1,647 tests, 82% coverage, gate ratcheted to 82%
- [x] pgvector backend for PostgreSQL-backed long-term memory (`backends/pgvector.py`)
- [x] SYMPHONY SharedReflectionPool for cross-iteration failure learning (`model_routing.py`)

## Deployment Status after Wave 17 (superseded by Wave 18 status above)

- **Image:** `registry.digitalocean.com/lula-orch/lula:v1.1.0`
- **Helm chart:** `oci://registry.digitalocean.com/lula-orch/lula:1.1.0`

## Wave 18 — Coverage, GLEAN, Edge (2026-04-02) ✅

- [x] GLEAN verification framework wired into executor (`glean.py`, `LG_GLEAN_ENABLED=true`)
- [x] SharedReflectionPool wired into planner for cross-iteration failure learning (`model_routing.py`)
- [x] 84% coverage gate — 1,788 tests, `--cov-fail-under=84` in pyproject.toml and CI
- [x] Edge deployment profile documented — k3s + Ollama single-node (`docs/deployment-edge.md`)
- [x] HPA tuning — production-ready scaling policies and PodDisruptionBudget active
- [x] v1.2.0 release — Helm chart at `oci://registry.digitalocean.com/lula-orch/lula:1.2.0`

## Deployment Status (2026-04-02)

- **Cluster:** DOKS `lula-prod` in nyc3, 2× `s-2vcpu-4gb` nodes, autoscale to 4
- **Image:** `registry.digitalocean.com/lula-orch/lula:v1.2.0`
- **Helm chart:** `oci://registry.digitalocean.com/lula-orch/lula:1.2.0`
- **Pods:** 2 orchestrator + 2 runner, all healthy
- **Ingress:** nginx + cert-manager, TLS via Let's Encrypt
- **DNS:** `lula.eiv.eng.br` A record created (NS delegation pending at registrar)
- **Direct access:** `curl -sk https://134.199.245.159/healthz -H "Host: lula.eiv.eng.br"`

## Wave 19 (Planned)

- [ ] Publish VS Code extension to marketplace (pending publisher documentation review)
- [ ] Complete DNS NS delegation at registrar for lula.eiv.eng.br
- [ ] Ratchet coverage to 85% (target: verifier.py, planner.py)
- [ ] Q-RAG embedder optimization — RL-trained multi-step retrieval
