# Architecture & Codebase Overview

This document provides comprehensive documentation on how the LG Orchestration Platform codebase currently works. The platform is designed as a split-architecture system: a Python-based intelligent orchestrator and a Rust-based secure tool runner.

## High-Level Design

The system separates "reasoning" from "execution" to achieve a secure, deterministic, and scalable workflow.

1. **Python Orchestrator (`py/`)**: Uses [LangGraph](https://github.com/langchain-ai/langgraph) to build a state machine (graph) that drives the agentic workflow. It handles planning, context building, and tool calling decisions.
2. **Rust Runner (`rs/runner/`)**: A sandbox web server built with `axum`. It exposes REST endpoints (`/v1/tools/execute`, `/v1/tools/batch_execute`) to safely execute filesystem and shell operations requested by the orchestrator.

## The Python Orchestrator (`py/`)

### State Management
The state of the agent is managed using Pydantic models defined in `py/src/lg_orch/state.py`. The primary state object is `OrchState`, which holds:
- `request`: The user's input.
- `plan`: The steps planned by the agent.
- `tool_results`: Artifacts and outputs from tool executions.
- `patches`: Code diffs to be applied.
- `verification`: Results from tests or linters.

### Graph Topology
The agent's thought process is defined as a directed graph in `py/src/lg_orch/graph.py`. The nodes represent steps in the workflow:
1. **ingest**: The entry point. Normalizes the request.
2. **policy_gate**: Enforces budgets and allowlists before proceeding.
3. **context_builder**: Gathers repository context and files.
4. **planner**: Analyzes the context and generates a structured `PlannerOutput` containing steps and tool calls.
5. **executor**: Uses the `RunnerClient` (`py/src/lg_orch/tools/runner_client.py`) to dispatch planned tool calls to the Rust runner over HTTP.
6. **verifier**: Executes verification commands (like `pytest` or `cargo test`) via the runner to ensure changes are correct.
7. **reporter**: Summarizes the final output and presents it to the user.

### Tool Client
The Python side never executes shell commands or writes files directly. Instead, it uses `RunnerClient` to send requests to the Rust server. It uses `httpx` and `tenacity` for resilient HTTP communication.

## The Rust Runner (`rs/runner/`)

The runner acts as a high-trust execution sandbox.

### Core Server
Defined in `rs/runner/src/main.rs`, it runs an HTTP server using `tokio` and `axum`. It accepts JSON requests containing tool execution instructions (`ToolExecuteRequest`).

### Security & Config
The runner uses `RunnerConfig` (`rs/runner/src/config.rs`) to enforce path boundaries (chroot-like behavior) and rate limits. It also verifies API keys.

### Available Tools
The logic for tools is located in `rs/runner/src/tools/`.
- **FS Tools (`fs.rs`)**: 
  - `read_file`: Reads a file if within the allowed root directory.
  - `list_files`: Lists files recursively or top-level.
  - `apply_patch`: Adds, updates, or deletes files safely.
- **Exec Tool (`exec.rs`)**:
  - `exec`: Spawns a subprocess. It uses a strict allowlist (`uv`, `python`, `pytest`, `ruff`, `mypy`, `cargo`, `git`) to prevent arbitrary command execution.

## Testing & CI
Both sides of the codebase are heavily tested:
- Python uses `pytest` and `hypothesis` for property-based testing.
- Rust uses `cargo test` with comprehensive unit tests for fs boundaries and allowed commands.

## Getting Started / Run Flow
When a user issues a command via the CLI (`uv run lg-orch run "task"`):
1. The Python CLI initializes the LangGraph state.
2. The orchestrator progresses through the nodes (ingest -> policy -> context -> plan).
3. The `executor` node hits the Rust runner's `/v1/tools/batch_execute` endpoint.
4. The Rust runner validates the request against its sandbox rules and performs the action, returning standard output, standard error, and exit codes.
5. The `verifier` checks the outcome.
6. The `reporter` prints the final result.
