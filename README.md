# Lula Platform

[![CI](https://github.com/christianmeurer/Lula/actions/workflows/ci.yml/badge.svg)](https://github.com/christianmeurer/Lula/actions/workflows/ci.yml)

A production-grade LangGraph orchestration platform (Python) paired with a high-trust restricted tool runner (Rust). Lula is designed for advanced autonomous coding and analysis workflows within secure enterprise environments.

## What Lula is

Lula is a full-stack agentic coding platform with:

- **Autonomous plan/execute/verify/recover loops** — explicit recovery contracts, failure fingerprinting, loop summaries, and acceptance criteria
- **Heterogeneous model routing** — `interactive`, `deep_planning`, and `recovery` lanes with compression pressure, cache affinity, and latency sensitivity
- **Algorithmic context compression** — stable-prefix / working-set split, token budgets, salience-scored fact packs
- **Full MCP protocol surface** — `tools/list`, `tools/call`, `resources/list`, `resources/read`, `prompts/list`, `prompts/get` with zero-trust schema hash pinning
- **Episodic + procedural memory** — cross-session recovery facts and verified tool sequences persisted in SQLite
- **Hardened Rust runner** — Linux namespace isolation (`unshare`), command allowlist, approval gates, redaction pipeline
- **Live run API** — `RemoteAPIService` with durable SQLite run store, multi-user namespace isolation, rate limiting, and a frontend SPA at `GET /`
- **VS Code extension** — configurable settings, inline diff, run history, remote API polling
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
│       → policy_gate → executor               │
│       → verifier → reporter                  │
│                                              │
│  Episodic memory  · Procedural cache         │
│  Context compression  · Recovery contracts   │
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
      executor                 │
          ↓                    │
      verifier                 │
          ↓ (retry)            │
     [policy_gate]             │
          │ (success)          │
          ↓                    ↓
       reporter ────────────> END
```

Recovery routing: `verifier` checks the outcome. If tools fail, it routes back to `policy_gate` for a bounded retry loop. `policy_gate` enforces loop budgets (`max_loops`). If budgets allow, `policy_gate` routes to `context_builder`, `router`, or `planner` based on the requested retry target. If budgets are exhausted or the verification succeeds, execution proceeds to `reporter`.

## Memory subsystems

| Subsystem | Storage | Scope |
|-----------|---------|-------|
| Working context | In-state | Current run |
| Loop summaries + facts | In-state | Current run |
| Episodic recovery facts | SQLite (`run_store_path`) | Cross-session |
| Procedural cache | SQLite (`procedure_cache_path`) | Cross-session |
| Checkpoints | SQLite (`checkpoint.db_path`) | Resumable runs |

## Security

- **Rust runner sandbox**: Linux namespace isolation via `unshare --pid --mount --net --fork` when `LG_RUNNER_LINUX_NAMESPACE_ENABLED=1`; falls back to process-level isolation.
- **MCP zero-trust**: optional `schema_hash` per server in config; runner refuses to inject tools if hash mismatches.
- **Constant-time auth**: bearer token comparison uses XOR-fold to prevent timing side-channels.
- **Redaction pipeline**: runner strips paths, usernames, and IP addresses from MCP responses before returning to orchestrator.
- **Approval gates**: `apply_patch` and state-modifying `exec` calls require explicit approval tokens.
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
| `POST` | `/v1/runs/{id}/cancel` | Cancel run |

## VS Code extension

The extension (`vscode-extension/`) provides:
- Run requests via command palette or panel input
- Local runner launch (`cargo run` or pre-built binary via `lula.runnerBinaryPath`)
- Remote API mode with live status polling
- Inline diff of `apply_patch` results
- Run history (last N runs, configurable)
- All values configurable via VS Code settings — no hardcoded addresses or keys

Settings: `lula.runnerBindAddress`, `lula.runnerApiKey`, `lula.runnerBinaryPath`, `lula.remoteApiBaseUrl`, `lula.remoteApiBearerToken`, `lula.showInlineDiff`, `lula.maxRunHistory`.

## Docker deployment

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

## Azure deployment

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
- `python-tests`: ruff lint, mypy, pytest (350 tests)
- `rust-tests`: clippy, cargo test (108 tests), fmt check
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

## Core documentation

- [`docs/architecture.md`](docs/architecture.md) — subsystem overview
- [`docs/sota_2026_plan.md`](docs/sota_2026_plan.md) — roadmap and gap analysis
- [`docs/platform_console.md`](docs/platform_console.md) — console and API reference
- [`docs/langgraph_plan.md`](docs/langgraph_plan.md) — LangGraph design notes
