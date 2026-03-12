# Lula Platform

[![CI](https://github.com/christianmeurer/lg-orchestration-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/christianmeurer/lg-orchestration-platform/actions/workflows/ci.yml)

A production-grade LangGraph orchestration scaffold (Python) paired with a high-trust restricted tool runner (Rust). Lula is designed for advanced agentic behavior within secure enterprise environments.

Key Features:
- **Repo-aware coding workflows** with intelligent context mapping
- **Iterative Reflection Loops** for autonomous self-correction (Verifier ↺ Planner)
- **Interactive Streaming** of agent thoughts and tool executions
- **Model Context Protocol (MCP)** integration for dynamic tool discovery
- **Deterministic tool execution** (`search_files`, `apply_patch`, `read_file`) within a strict Rust sandbox
- **Audit-friendly run traces** (JSON checkpointing)

Core docs:

- [`docs/langgraph_plan.md`](docs/langgraph_plan.md)
- [`docs/platform_console.md`](docs/platform_console.md)
- [`docs/architecture.md`](docs/architecture.md) (Codebase Overview)
- [`docs/sota_2026_plan.md`](docs/sota_2026_plan.md) (Enterprise Agentic Plan)

## Architecture

- Python orchestrator (LangGraph): [`py/src/lg_orch/graph.py`](py/src/lg_orch/graph.py)
- Rust runner (restricted tools & MCP client): [`rs/runner/src/main.rs`](rs/runner/src/main.rs)

The orchestrator never executes shell commands directly; it delegates execution safely to the Rust runner over HTTP.

## Quickstart

1) (Optional) Run the Rust tool runner

```bash
cd rs/runner
cargo run
```

2) Run the orchestrator CLI (Interactive Streaming is enabled by default)

```bash
cd py
uv sync
uv run lg-orch run "summarize repo" --trace
```

3) Export the orchestration graph (Mermaid)

```bash
cd py
uv run lg-orch export-graph
```

## DigitalOcean Serverless Inference (Claude Code-style planning)

The planner node can call DigitalOcean Serverless Inference through its OpenAI-compatible endpoint when the planner model provider is set to a non-local value.

1) Configure model routing in [`configs/runtime.dev.toml`](configs/runtime.dev.toml:1):

```toml
[models.planner]
provider = "remote_digitalocean"
model = "anthropic-claude-4.6-sonnet"
temperature = 0.1

[models.digitalocean]
base_url = "https://inference.do-ai.run/v1"
timeout_s = 60
```

2) Set your model access key in the environment (preferred):

```bash
set MODEL_ACCESS_KEY=your_digitalocean_model_access_key
```

3) Run the orchestrator:

```bash
cd py
uv run lg-orch run "implement a small feature and verify tests" --trace
```

Notes:
- If the planner completion fails or the API key is missing, planner falls back to deterministic local planning.
- The configured serverless endpoint is OpenAI-compatible (`/chat/completions`) and defaults to `https://inference.do-ai.run/v1`.
- Model access keys can also be provided with `DIGITAL_OCEAN_MODEL_ACCESS_KEY`.

## Trace dashboards

Generate a trace during a run:

```bash
cd py
uv run lg-orch run "summarize repo" --trace
```

Render a single trace as HTML:

```bash
cd py
uv run lg-orch trace-view ../artifacts/runs/run-<run_id>.json --format html --output ../artifacts/site/run-<run_id>.html
```

Build a static dashboard site from all run traces:

```bash
cd py
uv run lg-orch trace-site ../artifacts/runs --output-dir ../artifacts/site
```

Open [`artifacts/site/index.html`](artifacts/site/index.html) in a browser to browse generated dashboards and raw trace JSON.

## Security model (baseline)

- Root directory sandbox enforced by runner: requests resolve safely under `root_dir`.
- Tool allowlist:
  - FS: `read_file`, `list_files`, `apply_patch`, `search_files`
  - Exec: `exec` with strict command allowlist in [`allowed_cmd()`](rs/runner/src/tools/exec.rs)
  - MCP: `mcp_discover`, `mcp_execute`
- Network is denied by default (policy key exists in config).

Hardening to add for production:

- Path allowlist/denylist enforcement (glob-based) for write operations.
- Require explicit approval before `apply_patch` (orchestrator-side gate).
- Disable `git` by default in runner `exec` allowlist if not required.
- Run runner in an OS sandbox (container / low integrity token / namespaces).

## Roadmap (near-term)

- Implement complete JSON-RPC for MCP tool bridging
- Evolve the static trace dashboard into a served web UI and run API
- Add a job store for replay (SQLite → Postgres)

## Local verification

- Windows: [`scripts/dev.cmd`](scripts/dev.cmd)
- PowerShell: [`scripts/dev.ps1`](scripts/dev.ps1)
- Bash: [`scripts/dev.sh`](scripts/dev.sh)

## Local full-platform bootstrap (Windows)

Use [`scripts/bootstrap_local.cmd`](scripts/bootstrap_local.cmd) to launch the Rust runner, wait for health, sync Python deps, and run the orchestrator in one command.

```bat
scripts\bootstrap_local.cmd "implement a small feature and verify tests"
```

What it does:
- Starts runner on `127.0.0.1:8088`
- Waits for `http://127.0.0.1:8088/healthz`
- Runs `uv sync` in `py/`
- Runs `uv run lg-orch run ... --trace`

Notes:
- Uses `LG_PROFILE=dev` by default unless already set.
- Uses runner API key `dev-insecure` to match dev config.
- If `MODEL_ACCESS_KEY` is missing, planner remote mode falls back to deterministic local planning.

## Azure personal container hosting

[`Dockerfile`](Dockerfile) and [`scripts/start_remote_stack.sh`](scripts/start_remote_stack.sh) package the repo into a single personal-use container. The runner stays on `127.0.0.1:8088` inside the container and the public entrypoint is `uv run lg-orch serve-api` on port `8001` or `PORT` / `WEBSITES_PORT`.

1. Set deployment variables and any secrets in your shell:

   ```bat
   set AZ_RESOURCE_GROUP=rg-lula-personal
   set AZ_ACR_NAME=acrlulapersonal
   set AZ_CONTAINERAPP_NAME=lula-personal
   set LG_RUNNER_API_KEY=choose-a-runner-key
   set MODEL_ACCESS_KEY=your_model_key
   ```

2. Build and deploy with [`scripts/azure_deploy_personal.cmd`](scripts/azure_deploy_personal.cmd):

   ```bat
   scripts\azure_deploy_personal.cmd
   ```

3. Point the VS Code extension setting `lula.remoteApiBaseUrl` at the deployed HTTPS URL.

Notes:
- The helper script targets Azure Container Apps for a simple personal deployment path.
- The same image also fits Azure App Service custom containers if `WEBSITES_PORT=8001` is configured.
- Keep secrets in environment variables or Azure configuration, not in repo files.


