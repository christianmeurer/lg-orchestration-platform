# Architecture & Codebase Overview

This document provides comprehensive documentation on how the Lula Platform codebase currently works. The platform is designed as a split-architecture system: a Python-based intelligent orchestrator and a Rust-based secure tool runner.

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

## The Rust Runner (`rs/runner/`)

The runner acts as a high-trust execution sandbox.

### Core Server
Defined in `rs/runner/src/main.rs`, it runs an HTTP server using `tokio` and `axum`. It accepts JSON requests containing tool execution instructions (`ToolExecuteRequest`).

### Security & Config
The runner uses `RunnerConfig` (`rs/runner/src/config.rs`) to enforce path boundaries (chroot-like behavior) and rate limits. It also verifies API keys.

### Available Tools
The logic for tools is located in `rs/runner/src/tools/`.
- **FS Tools (`fs.rs`)**: 
  - `read_file`: Reads a file if within the allowed root directory. Text files are read as UTF-8, and `.pdf` files are extracted to text before returning output.
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
1. The Python CLI initializes the LangGraph state and checkpoint runtime.
2. The orchestrator progresses through the nodes (ingest -> policy -> context -> router -> planner -> coder).
3. The `executor` node hits the Rust runner's `/v1/tools/batch_execute` endpoint.
4. The Rust runner validates the request against its sandbox rules and performs the action, returning standard output, standard error, and exit codes.
5. If a mutation requires approval, the run can now suspend with durable approval state, checkpoint ids, and audit history exposed through the remote API.
6. The `verifier` checks the outcome. If tools fail or the loop budget isn't exhausted, execution loops back to `policy_gate` and can target `coder`, `planner`, `router`, or `context_builder` for the next bounded iteration.
7. The `reporter` prints the final result once verification passes, a run is rejected, or max loops are exhausted.
