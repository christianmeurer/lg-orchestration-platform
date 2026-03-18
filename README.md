# Lula Platform

[![CI](https://github.com/christianmeurer/Lula/actions/workflows/ci.yml/badge.svg)](https://github.com/christianmeurer/Lula/actions/workflows/ci.yml)

A production-grade LangGraph orchestration platform (Python) paired with a high-trust restricted tool runner (Rust). Lula is designed for advanced autonomous coding and analysis workflows within secure enterprise environments.

**Live deployment:** `https://lula-orch-y4t77.ondigitalocean.app` (DigitalOcean App Platform — DO Gradient AI inference, `openai-gpt-4.1` planner, `openai-gpt-4o-mini` router)

## What Lula is

Lula is a full-stack agentic coding platform with:

- **Autonomous plan/execute/code/verify/recover loops** — explicit recovery contracts, failure fingerprinting, loop summaries, acceptance criteria, and a dedicated coder specialist between planning and execution
- **Heterogeneous model routing** — `interactive`, `deep_planning`, and `recovery` lanes with compression pressure, cache affinity, and latency sensitivity
- **Algorithmic context compression** — stable-prefix / working-set split, token budgets, salience-scored fact packs
- **Full MCP protocol surface** — `tools/list`, `tools/call`, `resources/list`, `resources/read`, `prompts/list`, `prompts/get` with zero-trust schema hash pinning
- **Episodic + procedural memory** — cross-session recovery facts and verified tool sequences persisted in SQLite
- **Hardened Rust runner** — Linux namespace isolation (`unshare`), command allowlist, approval gates, redaction pipeline, prompt-injection detection
- **Live run API** — `RemoteAPIService` with durable SQLite run store, multi-user namespace isolation, rate limiting, suspended runs, approve/reject endpoints, and a frontend SPA at `GET /`
- **VS Code extension** — configurable settings, inline diff, run history, verifier report panel, remote API polling, suspended-run approval actions, and approval-history visibility
- **GitHub Actions CI** — Python lint/type/test, Rust clippy/test/fmt, Docker build, optional E2E with secrets

## Architecture

```
┌──────────────────────────────────────────────┐
│              VS Code Extension               │
│  (remote API polling, inline diff, history)  │
└────────────────────┬─────────────────────────┘
                     │ HTTP
┌────────────────────▼─────────────────────────┐
│          RemoteAPIService (Python)           │
│  /v1/runs  · /healthz  · GET / (SPA)        │
│  SQLite run store · multi-user namespaces    │
│  rate limiter · bearer auth                  │
└────────────────────┬─────────────────────────┘
                     │ subprocess
┌────────────────────▼─────────────────────────┐
│        LangGraph Orchestrator (Python)       │
│  router → context_builder → planner          │
│       → policy_gate → coder → executor       │
│       → verifier → reporter                  │
│                                              │
│  Episodic memory  · Procedural cache         │
│  Context compression  · Recovery contracts   │
│  Suspended approval state · Audit trail      │
└────────────────────┬─────────────────────────┘
                     │ HTTP
┌────────────────────▼─────────────────────────┐
│            Rust Runner (lg-runner)           │
│  exec (unshare sandbox)  · apply_patch       │
│  read_file  · search_files  · ast_index      │
│  mcp_discover  · mcp_execute                 │
│  mcp_resources_list  · mcp_resource_read     │
│  mcp_prompts_list  · mcp_prompt_get          │
│  approval gates  · redaction pipeline        │
└──────────────────────────────────────────────┘
```

## Quickstart

### 1. Start the Rust runner

```bash
cd rs
cargo run -- --bind 127.0.0.1:8088 --root-dir . --api-key dev-insecure
```

### 2. Run the orchestrator CLI

```bash
cd py
uv sync
uv run lg-orch run "summarize the repository structure" --trace
```

### 3. Start the remote API with live run viewer

```bash
cd py
uv run lg-orch serve-api --host 0.0.0.0 --port 8001
```

Open `http://localhost:8001` in a browser for the SPA run viewer.

### 4. Run with a real model (DigitalOcean Serverless)

```bash
export MODEL_ACCESS_KEY=your_do_model_key

# Configure model in configs/runtime.dev.toml:
# [models.planner]
# provider = "digitalocean"
# model = "meta-llama/Meta-Llama-3.1-70B-Instruct"

cd py
uv run lg-orch run "implement a new helper function" --trace
```

### 5. Run with a generic OpenAI-compatible endpoint

```bash
export OPENAI_COMPATIBLE_API_KEY=your_key

# Configure in configs/runtime.dev.toml:
# [models.openai_compatible]
# base_url = "https://api.openai.com/v1"

cd py
uv run lg-orch run "analyze the repository" --trace
```

## Configuration reference

All runtime config lives in `configs/runtime.{dev|stage|prod}.toml`.

| Section | Key fields |
|---------|-----------|
| `[models.router]` | `provider`, `model`, `temperature` |
| `[models.planner]` | `provider`, `model`, `temperature` |
| `[models.digitalocean]` | `base_url`, `timeout_s` |
| `[models.openai_compatible]` | `base_url`, `timeout_s` |
| `[models.routing]` | `local_provider`, `interactive_context_limit`, `deep_planning_context_limit`, `recovery_retry_threshold`, `default_cache_affinity` |
| `[budgets]` | `max_loops`, `max_tool_calls_per_loop`, `max_patch_bytes`, `stable_prefix_tokens`, `working_set_tokens` |
| `[policy]` | `network_default`, `require_approval_for_mutations`, `allowed_write_paths` |
| `[runner]` | `base_url`, `root_dir`, `api_key` |
| `[mcp]` | `enabled`, `servers.*` (with optional `schema_hash` for zero-trust pinning) |
| `[remote_api]` | `auth_mode`, `rate_limit_rps`, `run_store_path`, `procedure_cache_path`, `default_namespace` |
| `[checkpoint]` | `enabled`, `db_path`, `namespace` |
| `[trace]` | `enabled`, `output_dir`, `capture_model_metadata` |

## Orchestration graph

```
        ingest
          ↓
     policy_gate ──────────────┐ (budgets exhausted)
          ↓ (conditionally)    │
  context_builder              │
          ↓                    │
       router                  │
          ↓                    │
        planner                 │
          ↓                    │
         coder                  │
          ↓                    │
       executor                 │
          ↓                    │
      verifier                 │
          ↓ (retry)            │
     [policy_gate]             │
          │ (success)          │
          ↓                    ↓
       reporter ────────────> END
```

Recovery routing: `verifier` checks the outcome. If tools fail, it routes back to `policy_gate` for a bounded retry loop. `policy_gate` enforces loop budgets (`max_loops`). If budgets allow, `policy_gate` routes to `context_builder`, `router`, `planner`, or `coder` based on the requested retry target. If budgets are exhausted or the verification succeeds, execution proceeds to `reporter`.

## Memory subsystems

| Subsystem | Storage | Scope |
|-----------|---------|-------|
| Working context | In-state | Current run |
| Loop summaries + facts | In-state | Current run |
| Episodic recovery facts | SQLite (`run_store_path`) | Cross-session |
| Procedural cache | SQLite (`procedure_cache_path`) | Cross-session |
| Checkpoints | SQLite (`checkpoint.db_path`) | Resumable runs |

## Streaming inference

`InferenceClient.chat_completion_stream()` yields SSE tokens as an `AsyncGenerator[str, None]`, keeping the interactive lane non-blocking during graph execution. The `collect_stream()` helper concatenates all tokens into a single string for callers that need the full response. This is used in the interactive lane so that partial tokens are surfaced progressively rather than blocking the entire graph step.

## Security

- **Rust runner sandbox**: Linux namespace isolation via `unshare --pid --mount --net --fork` when `LG_RUNNER_LINUX_NAMESPACE_ENABLED=1`; falls back to process-level isolation.
- **Prompt injection detection**: `detect_prompt_injection` in `rs/runner/src/sandbox.rs` scans all subprocess argument strings for bidirectional Unicode overrides, RCE shell vectors, and cryptomining patterns before any exec call is permitted.
- **Subprocess environment isolation**: `env_clear()` is called before every exec, then an allowlist of safe variables is re-injected — no host environment leaks into sandboxed subprocesses.
- **Path traversal guard**: `resolve_under_root` / `normalize_path` in the runner rejects `../` traversal even for paths that do not yet exist on disk.
- **MCP zero-trust**: optional `schema_hash` per server in config; runner refuses to inject tools if hash mismatches.
- **Constant-time auth**: bearer token comparison uses XOR-fold to prevent timing side-channels.
- **Redaction pipeline**: runner strips paths, usernames, and IP addresses from MCP responses before returning to orchestrator.
- **Approval gates**: `apply_patch` and state-modifying `exec` calls require explicit approval tokens.
- **Governed autonomy**: suspended runs persist approval state, checkpoint identifiers, and approval history so they can be resumed or rejected through the API and clients.
- **Rate limiting**: token-bucket rate limiter on remote API (`rate_limit_rps`).
- **Circuit breaker**: `InferenceClient` opens after 5 consecutive failures; retries 429/5xx with backoff.

## Run API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Live SPA run viewer |
| `GET` | `/ui` | Same as `/` |
| `GET` | `/healthz` | Health check |
| `GET` | `/v1/runs` | List runs |
| `POST` | `/v1/runs` | Create run |
| `GET` | `/v1/runs/{id}` | Run detail + trace |
| `GET` | `/v1/runs/{id}/logs` | Stdout logs |
| `GET` | `/v1/runs/{id}/stream` | SSE live run updates |
| `POST` | `/v1/runs/{id}/cancel` | Cancel run |
| `POST` | `/v1/runs/{id}/approve` | Approve and resume a suspended run |
| `POST` | `/v1/runs/{id}/reject` | Reject a suspended run |

## VS Code extension

The extension (`vscode-extension/`) provides:
- Run requests via command palette or panel input
- Local runner launch (`cargo run` or pre-built binary via `lula.runnerBinaryPath`)
- Remote API mode with live status polling
- Inline diff of `apply_patch` results
- Verifier report panel — shows the verification JSON inline after each run
- Pending approval section with **Approve** / **Reject** buttons for gated exec calls
- Approval history and checkpoint visibility for suspended runs
- Run history (last N runs, configurable)
- All values configurable via VS Code settings — no hardcoded addresses or keys

Settings: `lula.runnerBindAddress`, `lula.runnerApiKey`, `lula.runnerBinaryPath`, `lula.remoteApiBaseUrl`, `lula.remoteApiBearerToken`, `lula.showInlineDiff`, `lula.maxRunHistory`.

## Deployment

### Docker

```bash
# Full image (Rust runner + Python API)
docker build -t lula:latest .

# Python-only image (API tier, no Rust build)
docker build -f Dockerfile.python -t lula-api:latest .
```

Environment variables for production containers:

```
LG_PROFILE=prod
MODEL_ACCESS_KEY=<model key>
LG_RUNNER_API_KEY=<runner key>
LG_REMOTE_API_AUTH_MODE=bearer
LG_REMOTE_API_BEARER_TOKEN=<api token>
LG_REMOTE_API_RATE_LIMIT_RPS=60
LG_RUNNER_LINUX_NAMESPACE_ENABLED=1
```

### DigitalOcean App Platform

```bash
DO_REGISTRY=lula-orch bash scripts/do_deploy.sh
```

### DOKS + gVisor (Kubernetes hardened)

```bash
DO_REGISTRY=lula-orch bash scripts/do_deploy_k8s.sh
```

Pods run under `runtimeClassName: gvisor` (see `infra/k8s/`) for kernel-level sandboxing on top of the Linux namespace isolation already enforced by the Rust runner.

### Azure Container Apps

```bat
set AZ_RESOURCE_GROUP=rg-lula
set AZ_ACR_NAME=acrlula
set AZ_CONTAINERAPP_NAME=lula
set LG_REMOTE_API_AUTH_MODE=bearer
set LG_REMOTE_API_BEARER_TOKEN=choose-a-token
set LG_RUNNER_API_KEY=choose-a-runner-key
set MODEL_ACCESS_KEY=your_model_key
scripts\azure_deploy_personal.cmd
```

## CI

GitHub Actions (`.github/workflows/ci.yml`):
- `python-tests`: ruff lint, mypy, pytest (~370 tests)
- `rust-tests`: clippy, cargo test (~124 tests), fmt check
- `docker-build`: combined and Python-only image builds
- `e2e-smoke`: E2E smoke tests against live model (gated on `MODEL_ACCESS_KEY` secret)
- `e2e.yml`: manual `workflow_dispatch` for full live model E2E

## Local development

```bash
# Windows
scripts\dev.cmd

# PowerShell
scripts\dev.ps1

# Bash
scripts/dev.sh

# Full bootstrap (start runner + run orchestrator)
scripts\bootstrap_local.cmd "your request here"
```

## Trace viewer

```bash
# Render single trace as HTML
uv run lg-orch trace-view artifacts/runs/run-<id>.json --format html

# Build static site from all traces
uv run lg-orch trace-site artifacts/runs --output-dir artifacts/site

# Serve live trace viewer
uv run lg-orch trace-serve artifacts/runs --port 8000
```

## What has been built (completed waves)

| Wave | What shipped |
|---|---|
| 1 — Docs sync | `README.md`, `docs/architecture.md`, `docs/platform_console.md` aligned with actual code |
| 2 — First product surface | SPA run viewer, trace dashboard, Mermaid graph export, console renderer |
| 3 — Run API + persistence | `RemoteAPIService`, SQLite run store, durable run listing, trace-backed detail views, cancellation |
| 4 — Provider expansion + routing | DigitalOcean Gradient AI + OpenAI-compatible providers, lane-aware routing, inference telemetry (latency, usage, cache headers) |
| 5 — Agent quality | Recovery packets, loop summaries, stable-prefix/working-set context compression with provenance, episodic recovery facts, procedural cache, eval suite |
| 6 — Execution quality | Concurrent runner fan-out, streaming inference in interactive paths, activated VS Code extension, real-world repair benchmark |
| 8 — Collaborative agents + governed autonomy (foundation) | Explicit coder node, typed handoff envelopes, coder-directed retries, suspended runs, approve/reject API, durable approval audit trail, SPA/VS Code approval flows |
| Deployment fixes | `LG_RUNNER_BASE_URL` env override, k8s runner split (`infra/k8s/runner-deployment.yaml` + `runner-service.yaml`), `do_deploy.sh` EV-ref preservation, DO model slug correction (`openai-gpt-4.1`), `runner.api_key` optional |

## Roadmap

### Wave 6 — execution quality, streaming, distribution

| Item | File | Impact |
|---|---|---|
| Concurrent Rust batch fan-out | [`rs/runner/src/main.rs`](rs/runner/src/main.rs) | Shipped with JoinSet fan-out for multi-tool batches |
| Streaming inference wired to interactive lane | [`py/src/lg_orch/tools/inference_client.py`](py/src/lg_orch/tools/inference_client.py) | Shipped across interactive planner/router paths |
| VSCode extension activation | [`vscode-extension/src/extension.ts`](vscode-extension/src/extension.ts) | Shipped with run status, verifier panel, approvals, and approval-history UX |
| Outcome quality benchmark | [`eval/run.py`](eval/run.py) + [`eval/tasks/real_world_repair.json`](eval/tasks/real_world_repair.json) | Shipped with repair benchmark and approval/suspend-resume eval scoring |

### Wave 7 — SOTA platform UX/UI

The most sophisticated agentic backend is invisible without a product-quality interface. Wave 7 targets a 2026-level immersive developer experience:

1. **Live run console with streaming timeline** — WebSocket/SSE-backed view; each graph node pulses as it activates, tool calls appear in real-time, lane is highlighted
2. **Animated agent graph visualization** — Mermaid or D3 force graph with active-node highlighting, edge animation in data-flow direction, recovery routing made visible
3. **Inline diff and verifier panel** — GitHub-style syntax-highlighted unified diff for `apply_patch` results; approval/reject buttons inline in the activity stream
4. **Run history and full-text search** — persistent left-panel run history with request text, duration, verification status, model used; suspended runs now surface checkpoint and approval state
5. **Design-system-quality layout** — Tailwind CSS + shadcn/ui (no Node.js build step in runtime image), VS Code dark theme parity, semantic color coding, responsive 1024–1440px
6. **VS Code extension premium UX** — vscode-webview-ui-toolkit, respects active color theme, inline gutter diffs, agent activity in sidebar

Design references: Vercel AI Playground (streaming token viz), Replit Ghostwriter (live agent trace), Cursor composer (multi-file diff approval), Linear (motion design polish).

Current state: Wave 7 is partially implemented. The live SPA and VS Code extension now expose real approval controls, approval history, checkpoint visibility, inline diffs, and verifier output; the remaining gap is deeper premium polish rather than basic operator functionality.

### Future pillars (from [`docs/sota_2026_plan.md`](docs/sota_2026_plan.md) §9)

| Pillar | Current state | Next step |
|---|---|---|
| Neurosymbolic vericoding | Verus stubs in `sandbox.rs`, `--features verify` in Cargo | Wire `cargo test --features verify` into verifier for changed `.rs` files |
| Tripartite memory | Episodic facts + procedure cache in SQLite | SQLite FTS for semantic cross-session recall |
| Cross-repo orchestration | `MetaOrchState` + `meta_graph.py` present | Git-worktree isolation + dependency-ordered sub-agent scheduler |
| Self-healing test loop | `test_failure_post_change` in verifier | Test-repair as first-class plan step |
| k8s hardware sandbox | gVisor manifests + deploy script ready | Provision DOKS cluster for enterprise deployment |

## Core documentation

- [`docs/architecture.md`](docs/architecture.md) — subsystem overview
- [`docs/deployment_digitalocean.md`](docs/deployment_digitalocean.md) — DigitalOcean App Platform and DOKS deployment guide
- [`docs/sota_2026_plan.md`](docs/sota_2026_plan.md) — roadmap and gap analysis (Waves 1–7)
- [`docs/platform_console.md`](docs/platform_console.md) — console and API reference
- [`docs/langgraph_plan.md`](docs/langgraph_plan.md) — LangGraph design notes
- [`docs/Innovative Agentic Coding Tool Concepts.pdf`](docs/Innovative%20Agentic%20Coding%20Tool%20Concepts.pdf) — field research: five next-generation architecture pillars
