# Architecture & Codebase Overview

**Version:** v1.2.0 — All waves complete through Wave 18. GLEAN verification wired into executor. SharedReflectionPool wired into planner. pgvector backend available. 1,788 tests, 84% coverage gate enforced. Edge deployment profile documented. Leptos SPA, VS Code extension, rich CLI, sqlite-vec vector indexing, and DiversityRoutingPolicy all implemented.

This document provides comprehensive documentation on how the Lula Platform codebase currently works. The platform is designed as a split-architecture system: a Python-based intelligent orchestrator and a Rust-based secure tool runner. All features described here are implemented and wired in the current codebase — no stubs or roadmap items are referenced as present unless explicitly marked.

## High-Level Design

The system separates "reasoning" from "execution" to achieve a secure, deterministic, and scalable workflow.

1. **Python Orchestrator (`py/`)**: Uses [LangGraph](https://github.com/langchain-ai/langgraph) to build a state machine (graph) that drives the agentic workflow. It handles planning, context building, and tool calling decisions.
2. **Rust Runner (`rs/runner/`)**: A sandbox web server built with `axum`. It exposes REST endpoints (`/v1/tools/execute`, `/v1/tools/batch_execute`) to safely execute filesystem and shell operations requested by the orchestrator.

## The Python Orchestrator (`py/`)

### State Management
The state of the agent is managed using Pydantic models defined in `py/src/lg_orch/state.py`. The primary state object is `OrchState`, which now carries not only planning/execution state but also explicit collaboration and control-plane state, including:
- `request`: The user's input.
- `plan`: The planned bounded workflow.
- `active_handoff`: the current specialist-to-specialist contract (for example planner → coder or verifier → coder).
- `tool_results`: Artifacts and outputs from tool executions.
- `verification`: Results from tests, critique, and recovery classification.
- `approvals` / `_approval_context`: approval and audit state for suspended / resumed runs.
- `_checkpoint`: resumability metadata including thread and checkpoint ids.

### Graph Topology
The agent's thought process is defined as a directed graph in `py/src/lg_orch/graph.py`. The nodes represent steps in the workflow:
1. **ingest**: The entry point. Normalizes the request.
2. **policy_gate**: Enforces budgets (e.g. `max_loops`) and allowlists. Conditionally routes back to `context_builder`, `router`, `planner`, or proceeds to `reporter` if budgets are exhausted.
3. **context_builder**: Gathers repository context, AST summaries, and semantic hits.
4. **router**: Decides model routing lanes based on task and context needs.
5. **planner**: Analyzes the context and generates a structured `PlannerOutput` containing steps, verification calls, and specialist handoff contracts.
6. **coder**: Consumes planner handoffs, prepares a bounded execution handoff for the executor, and keeps patch work explicit rather than implicit inside planning.
7. **executor**: Uses the `RunnerClient` (`py/src/lg_orch/tools/runner_client.py`) to dispatch planned tool calls to the Rust runner over HTTP.
8. **verifier**: Evaluates the results of the execution. If verification fails, it routes back to `policy_gate` for context reset and retry (forming a bounded verify/retry loop). It can now target `coder`, `planner`, `router`, or `context_builder` depending on failure class.
9. **reporter**: Summarizes the final output and presents it to the user.

### Tool Client
The Python side never executes shell commands or writes files directly. Instead, it uses `RunnerClient` to send requests to the Rust server. It uses `httpx` and `tenacity` for resilient HTTP communication.

## The Python API Layer (`py/src/lg_orch/api/`)

The `remote_api.py` monolith (previously ~2,045 lines) was decomposed in Wave 2 into four focused submodules under `py/src/lg_orch/api/`:

| Module | Responsibility |
|---|---|
| `metrics.py` | Prometheus counter and histogram exposition; `/metrics` endpoint handler |
| `streaming.py` | SSE stream management — per-run event queues, chunk serialisation, client lifecycle |
| `approvals.py` | Approval suspend/resume API — token issuance, HMAC-SHA256 validation, approve/reject endpoints |
| `service.py` | Top-level `RemoteAPIService` wiring: mounts the sub-routers, initialises rate-limit middleware, and owns the server lifecycle |
| `admin.py` | Healing loop admin routes — force-trigger, status query, and loop-budget override endpoints added in Wave B |

The `remote_api.py` facade in `py/src/lg_orch/remote_api.py` re-exports from these submodules for backward-compatibility. The internal request-dispatch logic was refactored from a 234-line `if/elif` chain to a dispatch table of 12 dedicated handler functions, eliminating the linear scan and making handler registration explicit.

## CLI Commands Subpackage (`py/src/lg_orch/commands/`)

`main.py` was decomposed in Wave B into a thin dispatcher (<200 lines) that delegates all CLI entry points to four focused command modules:

| Module | Entry point | Responsibility |
|---|---|---|
| `run.py` | `lg-orch run` | Graph execution — wires state, checkpoint store, and invokes the LangGraph runner |
| `serve.py` | `lg-orch serve` | Remote API server startup — binds the `RemoteAPIService` and manages the server lifecycle |
| `trace.py` | `lg-orch trace` | Trace inspection — loads and pretty-prints persisted OTel/structlog trace records |
| `heal.py` | `lg-orch heal` | Healing loop management — triggers manual repair cycles and reports loop status |

The `commands/__init__.py` re-exports all four entry points so that `main.py`'s `typer` app can register them with a single import.

## Configuration Overlay (`pydantic-settings`)

Three `pydantic-settings` classes layer on top of the TOML config files, giving environment variable precedence without modifying config files:

| Class | Env prefix | Overrides |
|---|---|---|
| `RunnerSettings` | `LG_RUNNER_` | `base_url`, `api_key`, `timeout_s`, `max_retries` |
| `AuthSettings` | `LG_AUTH_` | `mode`, `bearer_token`, `jwks_url`, `hmac_secret` |
| `CheckpointSettings` | `LG_CHECKPOINT_` | `backend`, `sqlite_path`, `redis_url`, `postgres_dsn` |

This makes Kubernetes Secret injection work without patching TOML files at deploy time — inject `LG_RUNNER_BASE_URL`, `LG_AUTH_MODE`, `LG_CHECKPOINT_BACKEND`, etc. as pod environment variables and the overlay picks them up automatically.

## Checkpointing Backends (`py/src/lg_orch/backends/`)

The checkpointing subsystem was previously implemented as a 1,507-line monolith (`checkpointing.py`) with three backends sharing duplicated `_parse_config()` logic. It has been split into a `backends/` subpackage:

| Module | Backend | Notes |
|---|---|---|
| `backends/_base.py` | Abstract base | `CheckpointBackend` ABC; shared `_parse_config()` helper |
| `backends/sqlite.py` | SQLite (WAL mode) | Default for local/dev; file path via `LG_CHECKPOINT_SQLITE_PATH` |
| `backends/redis.py` | Redis (async) | TTL-based expiry; `LG_CHECKPOINT_REDIS_URL` |
| `backends/postgres.py` | PostgreSQL | `LG_CHECKPOINT_POSTGRES_DSN`; advisory locks for concurrent access |

`py/src/lg_orch/checkpointing.py` is retained as a backward-compatibility shim that re-exports the public API from the `backends/` subpackage. New code should import directly from `lg_orch.backends`.

## Shared Node Utilities (`py/src/lg_orch/nodes/_utils.py`)

A `_utils.py` module under `py/src/lg_orch/nodes/` centralises utilities previously duplicated across executor, verifier, context_builder, router, and planner:

- `validate_base_url(url: str) -> str` — normalises and validates the runner base URL, raising `ConfigError` on malformed input.
- `extract_json_block(text: str) -> dict` — strips markdown fences and parses the first JSON object from a model response.
- `resolve_inference_client(config: LulaConfig) -> InferenceClient` — constructs the appropriate `InferenceClient` variant from the active configuration profile.

## The Rust Runner (`rs/runner/`)

The runner acts as a high-trust execution sandbox.

### Core Server
Defined in `rs/runner/src/main.rs`, it runs an HTTP server using `tokio` and `axum`. It accepts JSON requests containing tool execution instructions (`ToolExecuteRequest`).

### Security & Config
The runner uses `RunnerConfig` (`rs/runner/src/config.rs`) to enforce path boundaries (chroot-like behavior) and rate limits. It also verifies API keys. The single source of truth for the exec command allowlist (`ALLOWED_EXEC_COMMANDS`) lives in `rs/runner/src/config.rs`; no other module defines or duplicates this list.

### Per-Request Tool Context
Each request constructs a `ToolContext` struct that carries the undo pointer for that request's scope. The previous `LAST_UNDO_POINTER` global has been removed; there is no shared mutable state across concurrent batch requests in the tool dispatch path.

### HMAC Approval Protocol
The Rust runner validates approval tokens in `rs/runner/src/auth.rs` using HMAC-SHA256 with constant-time comparison (`subtle::ConstantTimeEq`) and TTL enforcement. The Python orchestrator (`py/src/lg_orch/auth.py`, `py/src/lg_orch/api/approvals.py`) now issues and verifies tokens using the same HMAC-SHA256 scheme, bringing both layers to parity. JWT verification uses `PyJWT[crypto]>=2.8,<3` (replacing the unmaintained `python-jose` library).

### Supply-Chain Scanning
`rs/deny.toml` configures `cargo-deny` for the Rust workspace. It enforces license allowlists and advisory database checks. The CI workflow runs `cargo deny check` on every pull request.

### Sandbox Tier Selection

`SandboxPreference::Auto` selects the highest available tier at startup:

1. If a Firecracker rootfs and kernel image are configured → `MicroVmEphemeral`
2. Else if `unshare` is found on `PATH` → **`LinuxNamespace`** (default on standard Linux installs)
3. Else → `SafeFallback` (process isolation only; not the default when `unshare` is available)

Prior to the fix sprint the `Auto` path fell through to `SafeFallback` on any host where `microvm_enabled = false` and `ns_enabled = false`, meaning most deployments ran with no kernel-level containment. Auto-detection of `unshare` now ensures `LinuxNamespace` is used by default on capable hosts.

### Available Tools
The logic for tools is located in `rs/runner/src/tools/`.
- **FS Tools (`fs.rs`)**:
  - `read_file`: Reads a file if within the allowed root directory. Text files are read as UTF-8, and `.pdf` files are extracted to text before returning output.
  - `list_files`: Lists files recursively or top-level.
  - `apply_patch`: Adds, updates, or deletes files safely.
- **Exec Tool (`exec.rs`)**:
  - `exec`: Spawns a subprocess. It uses the allowlist defined in `config.rs` (`uv`, `python`, `pytest`, `ruff`, `mypy`, `cargo`, `git`) to prevent arbitrary command execution.
  - On the `MicroVmEphemeral` backend, `exec` does **not** run the command on the host. Instead it sends a `GuestCommandRequest` to the guest agent running inside the Firecracker microVM over `AF_VSOCK` (CID 3, port 52525). On non-Linux hosts the path returns `ApiError::BadRequest` with a descriptive platform-not-supported message.

### Firecracker vsock guest agent

The `rs/guest-agent/` workspace member provides the `lula-guest-agent` binary that runs inside the Firecracker rootfs.

| Aspect | Detail |
|---|---|
| Transport (Linux) | `AF_VSOCK` socket, port configurable via `GUEST_AGENT_PORT` (default 52525) |
| Transport (test/macOS) | Unix domain socket at `GUEST_AGENT_SOCK` (default `/tmp/lula-agent.sock`) |
| Protocol | Newline-delimited JSON (one request → one response per connection) |
| Request shape | `{"cmd":"cargo","args":[...],"cwd":"/workspace","env":{...},"timeout_ms":30000}` |
| Response shape | `{"ok":true,"exit_code":0,"stdout":"...","stderr":"...","timing_ms":1234}` |

The host-side vsock client lives in `rs/runner/src/vsock.rs`. On Linux it opens the AF_VSOCK device and communicates with the guest over a `UnixStream`-based I/O wrapper (the previous implementation incorrectly wrapped the raw AF_VSOCK file descriptor in `std::net::TcpStream`, which is undefined behavior per the type system). Requests are performed as a single exchange per connection, wrapped in a tokio timeout. The `FirecrackerVmm` struct in `sandbox.rs` carries a `cid: u32` field (default 3) populated after `configure_and_start` configures the vsock device via `PUT /vsock`.

**Linux-only constraint:** All `AF_VSOCK` socket code is guarded by `#[cfg(target_os = "linux")]`. The runner and guest-agent compile and test cleanly on Windows/macOS for development purposes; the `MicroVmEphemeral` execution path returns a graceful `BadRequest` error on those platforms.

### Guest rootfs build pipeline

The `rs/guest-agent/Dockerfile.rootfs` multi-stage Docker build produces a bootable 256 MiB ext4 image containing a minimal Alpine Linux environment and the statically-linked (`musl`) `lula-guest-agent` binary.

| Aspect | Detail |
|---|---|
| Build script | `scripts/build_guest_rootfs.sh` (Bash) / `scripts/build_guest_rootfs.cmd` (Windows) |
| Output | `artifacts/rootfs.ext4` — 256 MiB ext4, musl-linked Alpine + `lula-guest-agent` |
| Init script | `/sbin/init` mounts `proc`, `sysfs`, `devtmpfs`, `devpts` then `exec`s `lula-guest-agent` |
| CI | `build-guest-rootfs` job in `ci.yml` — runs on every `main` push or when `rs/guest-agent/` changes; uploads `artifacts/rootfs.ext4` as a GitHub Actions artifact |
| Release | `build-guest-rootfs` job in `release.yml` — attaches `rootfs.ext4` to each GitHub Release as a downloadable asset |
| Deployment | Mount at `/opt/lula/rootfs.ext4` via Kubernetes `HostPath` volume (`infra/k8s/runner-deployment.yaml`) |
| Kernel | Download from `firecracker-microvm/firecracker` releases; place at `/opt/lula/vmlinux` on each node |
| Env vars | `LG_RUNNER_ROOTFS_IMAGE` (default `artifacts/rootfs.ext4`), `LG_RUNNER_KERNEL_IMAGE` (default `artifacts/vmlinux`) |

Build steps (requires Docker with BuildKit):

```bash
# Linux / macOS
bash scripts/build_guest_rootfs.sh

# Windows
scripts\build_guest_rootfs.cmd
```

## Leptos SPA (`rs/spa-leptos/`)

The primary web frontend is a Leptos single-page application compiled to WebAssembly via [Trunk](https://trunkrs.dev/). It runs in CSR (client-side rendering) mode and communicates with the Python API over HTTP and SSE.

| Aspect | Detail |
|---|---|
| Framework | Leptos 0.7, compiled to `wasm32-unknown-unknown` |
| Build tool | Trunk (`trunk serve` for dev, `trunk build --release` for production) |
| Design system | Cyberpunk Minimal — dark backgrounds, neon accents, monospace typography |
| Pages | Dashboard, Run Detail, Settings, New Run |
| Streaming | Signal-based SSE subscription; reactive UI updates on each event |
| Approvals | Inline approve/reject modals with HMAC token flow and diff preview |
| Theme | CSS custom properties with dark/light hybrid mode |

The SPA is built in CI (`build-spa` job in `ci.yml`) and the dist output is uploaded as an artifact for downstream jobs.

## VS Code Extension (`vscode-extension/`)

The VS Code extension provides a native IDE integration for Lula. It is built with TypeScript and bundled with esbuild.

| Aspect | Detail |
|---|---|
| Build tool | esbuild (`node esbuild.js`) |
| Commands | `lula.runTask`, `lula.showRuns`, `lula.configure`, `lula.approveRun` |
| Webview | HTML panel with live SSE streaming, styled to match Cyberpunk Minimal |
| Communication | `postMessage` protocol between extension host and webview |
| SSE proxy | Extension host subscribes to API SSE and forwards events to webview |
| Auth storage | VS Code `SecretStorage` for API tokens |
| Publishing | `vscode-publish.yml` workflow, triggered on `vscode-v*` tags |

## Rich CLI (`py/src/lg_orch/commands/`)

The CLI uses the `rich` library for formatted terminal output. Key features:

- **Themed console:** Styled panels, tables, progress bars, and syntax-highlighted code blocks
- **Log separation:** Structured log output goes to stderr; results go to stdout for clean piping
- **Color output:** Status indicators, severity-colored messages, and branded header panels

## Eval Framework (`eval/run.py`)

The eval runner gained the following capabilities in Wave D:

- **`--swe-bench PATH`** — loads a SWE-bench JSONL file (one task object per line). Each line's `instance_id`, `problem_statement`, and `patch` fields are mapped to Lula's internal task format. The loader respects `--swe-bench-limit N` to cap the number of tasks for fast iteration.
- **`resolved_rate` metric** — the summary table now reports `resolved_rate = resolved / total` alongside the existing `pass@k` columns. Nightly CI enforces a minimum threshold of `0.30` on `real_world_repair.json`.
- **Benchmark class grouping** — the `pass@k` table groups tasks by their `class` field (e.g. `repair`, `analysis`, `refactor`) so per-class pass rates are visible alongside the aggregate.
- **`--dry-run` flag** — prints the resolved task list (IDs, requests, golden file paths) without invoking the LangGraph graph. Useful for verifying loader output and fixture availability before a full eval run.

## Testing & CI

Both sides of the codebase are heavily tested:
- Python uses `pytest` and `hypothesis` for property-based testing.
- Rust uses `cargo test` with comprehensive unit tests for fs boundaries and allowed commands.
- **1,788 tests** total; **84% coverage** enforced via `--cov-fail-under=84` in `pyproject.toml` and CI.
- 7 CI jobs all green: Python lint/test, Rust lint/test, SPA build, security audit, eval canary.

## Long-Term Memory (`py/src/lg_orch/long_term_memory.py`)

The tripartite long-term memory store (semantic/episodic/procedural) uses SQLite with FTS5 and WAL mode. Vector search is now backed by **sqlite-vec**, which provides indexed approximate nearest-neighbor queries. This replaces the previous O(n) numpy cosine scan. When sqlite-vec is not available, the system falls back transparently to numpy-based search.

The embedding provider is configurable via `LG_EMBED_PROVIDER`:
- `ollama` — Uses the `OllamaEmbedder` with `nomic-embed-text` (default in production, deployed as a sidecar)
- `openai` — Uses the OpenAI embeddings API
- `stub` — Hash-based stub embedder for testing (semantically meaningless)

### pgvector Backend (`py/src/lg_orch/backends/pgvector.py`)

For teams running PostgreSQL, the `pgvector` backend provides a PostgreSQL-native vector index using the `pgvector` extension. Select it with `LG_CHECKPOINT_BACKEND=postgres` and ensure `pgvector` is installed in the target PostgreSQL instance (`CREATE EXTENSION vector`).

| Backend | Index type | Use case |
|---|---|---|
| sqlite-vec | ANN (approximate NN) | Default; embedded, zero external deps |
| pgvector | IVFFlat / HNSW | PostgreSQL deployments; multi-instance shared memory |

## Model Routing (`py/src/lg_orch/model_routing.py`)

Model routing supports multiple policies:

- **SlaRoutingPolicy:** P95 latency tracking with automatic fallback when thresholds are exceeded.
- **DiversityRoutingPolicy:** SYMPHONY-inspired heterogeneous model selection via round-robin routing across different model providers. Opt-in via `LG_MODEL_DIVERSITY=true`. Wired into the planner via `get_routing_policy()` factory.
- **Temperature diversity mixin:** Spreads temperature values across model calls for pluralistic alignment.

## GLEAN Verification Framework (`py/src/lg_orch/glean.py`)

GLEAN (Guideline-grounded Evaluation of Agent Actions) is wired into the executor node. It runs pre- and post-tool checks against a set of `DEFAULT_GUIDELINES` — a curated list of safety and correctness invariants.

- **Opt-in:** Set `LG_GLEAN_ENABLED=true` to activate.
- **Pre-check:** Before each tool call, GLEAN audits the action against the active guidelines and can veto operations that violate them.
- **Post-check:** After execution, GLEAN verifies the tool output conforms to expected invariants, flagging deviations for the verifier.
- **11 unit tests** cover the guideline evaluation and veto paths.

## SharedReflectionPool (`py/src/lg_orch/model_routing.py`)

The SYMPHONY `SharedReflectionPool` is wired into the planner node for cross-iteration failure learning.

- The pool accumulates structured failure records from the verifier across iterations of the plan/execute/verify loop.
- On each planning invocation, the planner queries the pool for similar past failures and injects relevant lessons into the planning prompt.
- This enables the planner to avoid repeating the same mistakes across loop iterations within a single run, and optionally across runs (when the pool is persisted).
- Implemented in `model_routing.py`; activated automatically when the planner invokes `get_routing_policy()`.

## Edge Deployment

For air-gapped or resource-constrained environments, Lula ships an edge deployment profile targeting k3s + Ollama. See [`docs/deployment-edge.md`](deployment-edge.md) for the full guide.

Key characteristics:
- **Single-node k3s** — orchestrator and runner co-located; no cloud dependencies after image pull.
- **Ollama local inference** — all LLM calls served locally via Ollama; `nomic-embed-text` for embeddings.
- **sqlite-vec only** — pgvector not required; SQLite WAL mode checkpoint store.
- **SafeFallback / LinuxNamespace sandbox** — Firecracker not required (no KVM needed).

---

## Getting Started / Run Flow
When a user issues a command via the CLI (`uv run lg-orch run "task"`):
1. The Python CLI initializes the LangGraph state and checkpoint runtime.
2. The orchestrator progresses through the nodes (ingest -> policy -> context -> router -> planner -> coder).
3. The `executor` node hits the Rust runner's `/v1/tools/batch_execute` endpoint.
4. The Rust runner validates the request against its sandbox rules and performs the action, returning standard output, standard error, and exit codes.
5. If a mutation requires approval, the run can now suspend with durable approval state, checkpoint ids, and audit history exposed through the remote API.
6. The `verifier` checks the outcome. If tools fail or the loop budget isn't exhausted, execution loops back to `policy_gate` and can target `coder`, `planner`, `router`, or `context_builder` for the next bounded iteration.
7. The `reporter` prints the final result once verification passes, a run is rejected, or max loops are exhausted.
