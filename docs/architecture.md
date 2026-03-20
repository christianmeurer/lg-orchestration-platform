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

## The Python API Layer (`py/src/lg_orch/api/`)

The `remote_api.py` monolith (previously ~2,045 lines) was decomposed in Wave 2 into four focused submodules under `py/src/lg_orch/api/`:

| Module | Responsibility |
|---|---|
| `metrics.py` | Prometheus counter and histogram exposition; `/metrics` endpoint handler |
| `streaming.py` | SSE stream management — per-run event queues, chunk serialisation, client lifecycle |
| `approvals.py` | Approval suspend/resume API — token issuance, HMAC-SHA256 validation, approve/reject endpoints |
| `service.py` | Top-level `RemoteAPIService` wiring: mounts the sub-routers, initialises rate-limit middleware, and owns the server lifecycle |

The `remote_api.py` facade in `py/src/lg_orch/remote_api.py` re-exports from these submodules for backward-compatibility.

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
The Rust runner validates approval tokens in `rs/runner/src/auth.rs` using HMAC-SHA256 with constant-time comparison (`subtle::ConstantTimeEq`) and TTL enforcement. The Python orchestrator (`py/src/lg_orch/auth.py`, `py/src/lg_orch/api/approvals.py`) now issues and verifies tokens using the same HMAC-SHA256 scheme, bringing both layers to parity.

### Supply-Chain Scanning
`rs/deny.toml` configures `cargo-deny` for the Rust workspace. It enforces license allowlists and advisory database checks. The CI workflow runs `cargo deny check` on every pull request.

### Available Tools
The logic for tools is located in `rs/runner/src/tools/`.
- **FS Tools (`fs.rs`)**:
  - `read_file`: Reads a file if within the allowed root directory. Text files are read as UTF-8, and `.pdf` files are extracted to text before returning output.
  - `list_files`: Lists files recursively or top-level.
  - `apply_patch`: Adds, updates, or deletes files safely.
- **Exec Tool (`exec.rs`)**:
  - `exec`: Spawns a subprocess. It uses the allowlist defined in `config.rs` (`uv`, `python`, `pytest`, `ruff`, `mypy`, `cargo`, `git`) to prevent arbitrary command execution.

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
