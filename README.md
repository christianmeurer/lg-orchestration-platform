# LG Orchestration Platform

[![CI](https://github.com/christianmeurer/lg-orchestration-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/christianmeurer/lg-orchestration-platform/actions/workflows/ci.yml)

LangGraph orchestration scaffold (Python) + restricted tool runner (Rust) designed for:

- repo-aware coding workflows
- deterministic tool execution
- audit-friendly run traces
- safe-by-default policies (runner boundary)

Core docs:

- [`docs/langgraph_plan.md`](docs/langgraph_plan.md:1)
- [`docs/platform_console.md`](docs/platform_console.md:1)

## Architecture

- Python orchestrator (LangGraph): [`py/src/lg_orch/graph.py`](py/src/lg_orch/graph.py:1)
- Rust runner (restricted tools): [`rs/runner/src/main.rs`](rs/runner/src/main.rs:1)

The orchestrator never executes shell commands directly; it calls the runner via HTTP.

## Quickstart

1) (Optional) Run the Rust tool runner

```bash
cd rs/runner
cargo run
```

2) Run the orchestrator CLI

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

- Root directory sandbox enforced by runner: requests resolve under `root_dir`.
- Tool allowlist:
  - FS: `read_file`, `list_files`, `apply_patch`
  - Exec: `exec` with command allowlist in [`allowed_cmd()`](rs/runner/src/tools/exec.rs:24)
- Network is denied by default (policy key exists in config); runner currently does not expose network tools.

Hardening to add for production:

- Path allowlist/denylist enforcement (glob-based) for write operations.
- Require explicit approval before `apply_patch` (orchestrator-side gate).
- Disable `git` by default in runner `exec` allowlist if not required.
- Run runner in an OS sandbox (container / low integrity token / namespaces).

## Roadmap (near-term)

- Make runner protocol explicitly versioned (schema ids in envelopes)
- Add a minimal web UI for graph/timeline/artifacts (consuming run traces)
- Add a job store for replay (SQLite → Postgres)

## Local verification

- Windows: [`scripts/dev.cmd`](scripts/dev.cmd:1)
- PowerShell: [`scripts/dev.ps1`](scripts/dev.ps1:1)
- Bash: [`scripts/dev.sh`](scripts/dev.sh:1)


