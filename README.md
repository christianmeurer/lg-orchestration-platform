# LG Orchestration Platform

[![CI](https://github.com/christianmeurer/lg-orchestration-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/christianmeurer/lg-orchestration-platform/actions/workflows/ci.yml)

A production-grade LangGraph orchestration scaffold (Python) paired with a high-trust restricted tool runner (Rust). Designed for advanced agentic behavior (similar to Roo Code and Claude Code) within secure enterprise environments.

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
- Add a minimal web UI for graph/timeline/artifacts (consuming run traces)
- Add a job store for replay (SQLite → Postgres)

## Local verification

- Windows: [`scripts/dev.cmd`](scripts/dev.cmd)
- PowerShell: [`scripts/dev.ps1`](scripts/dev.ps1)
- Bash: [`scripts/dev.sh`](scripts/dev.sh)


