# LangGraph Orchestration Plan (SOTA 2026) — Python + Rust “General Coding” Assistant

## 0) Objectives

- Provide a production-grade orchestration pattern for: planning, tool execution, repo-aware coding, verification, and reporting.
- Optimize for: correctness, reproducibility, bounded autonomy, observability, and low operational drag.
- Support multi-language (Python + Rust) with deterministic tool runners and strong CI gates.

This plan targets “SOTA 2026” practices used by high-performing orchestration teams:

- **Typed state + structured outputs** everywhere (minimize prompt drift and key drift).
- **Durable, resumable workflows** (checkpointing + replay + audit).
- **Policy-driven autonomy** (budgets, allowlists, approvals).
- **Eval-driven iteration** (offline regression suite + online canaries).
- **Separation of concerns**: reasoning (planner) vs execution (runner) vs verification (CI mirrors).

Non-goals:

- Unbounded “auto-coding” without verification.
- Direct execution on the host outside an allowlisted sandbox.

## 1) Reference Stack (2026)

### Python (orchestrator)

- Runtime: Python 3.12+
- Orchestration: LangGraph
- Web API: FastAPI (optional; for service mode)
- Settings: pydantic-settings
- Networking: httpx
- Retries: tenacity
- Logging/tracing: structlog + OpenTelemetry
- Tool sandbox client: HTTP/gRPC to a runner (Rust recommended)
- Packaging/Env: uv
- Lint/Format: ruff
- Types: mypy (strict)
- Tests: pytest + hypothesis

Optional, common additions (depending on product constraints):

- AuthN/AuthZ: OAuth2/JWT (FastAPI dependencies) + service-to-service mTLS
- Persistence: Postgres (conversation/workflow store) + SQLite for local/dev
- Queue: Redis/KeyDB or NATS (for background jobs)
- RAG indexing: pgvector/Qdrant + BM25 (tantivy/Meilisearch/SQLite FTS)

### Rust (tool runner + high-trust execution)

- Runtime: tokio
- HTTP: axum + tower
- Serialization: serde
- HTTP client: reqwest
- DB: sqlx (Postgres) or sqlite
- Errors: thiserror (lib), anyhow (bin)
- Observability: tracing + opentelemetry
- CLI glue: clap
- Tests: proptest + insta (snapshots)

Runner hardening options (recommended for multi-tenant):

- Linux: namespaces/seccomp/AppArmor, cgroups v2
- Windows: Job Objects + low integrity token (where feasible) + strict ACLs
- Container: rootless Docker/Podman with no network by default

### Infra (practical defaults)

- Cache: Redis (optional) or in-process + SQLite checkpointing
- Checkpoint store: SQLite (single node) → Postgres (multi node)
- Vector store (RAG): pgvector or Qdrant
- Artifact storage: S3-compatible (optional)
- CI: GitHub Actions / GitLab CI

## 2) High-level Architecture

Split “reasoning” and “execution”:

- Python service: LangGraph orchestrator + policy + retrieval + planning.
- Rust service: restricted tool runner (filesystem patch application, `cargo`, `uv`, `pytest`, `ruff`, etc.).

This yields:

- Safer execution boundaries.
- Reusable execution layer across multiple orchestrators.
- Better concurrency and IO performance.

Also recommended:

- **Two-lane operation**:
  - *Interactive lane*: lower latency, smaller budgets, human approvals on mutations.
  - *Batch lane*: deeper analysis, more retrieval, heavier verification.
- **Durable job model** (every request becomes a job with a stable ID, replayable steps, and artifacts).

## 3) LangGraph Design

### 3.1 State schema

Represent state as a strict Pydantic model (preferred) to avoid accidental key drift.

- `request`: raw user message
- `intent`: enum (code_change, analysis, research, question, refactor, debug)
- `repo_context`: paths, file summaries, dependency manifests
- `facts`: retrieved snippets with source references
- `plan`: ordered steps (tool calls + expected outcomes)
- `tool_results`: transcripts (stdout/stderr, exit codes, file diffs)
- `patches`: validated diff payloads
- `verification`: test/lint/build results
- `final`: user-facing answer
- `guards`: policy decisions (allowed tools, redactions)

Add for 2026 production needs:

- `budgets`: token/tool/time budgets + remaining counters
- `approvals`: required approvals + granted approvals (human / policy engine)
- `security`: risk score, sensitive paths detected, redaction events
- `telemetry`: trace IDs, span links, evaluation tags

### 3.2 Node inventory (recommended)

1. **ingest**
   - Normalize input, detect repo presence, identify language(s), gather constraints.

2. **policy_gate**
   - Enforce tool allowlist, secret redaction, and “no network” defaults.
   - Decide whether request requires confirmation.
   - Apply budgets (max tool calls, max patch size, max runtime).

3. **context_builder**
   - Collect local repo context (manifest files, tree summaries, open tabs).
   - Optionally run retrieval (docs/notes/ADR, prior runs).

4. **router**
   - Route to: `research_track` vs `coding_track` vs `qa_track`.
   - Choose model tier (cheap router model vs expensive planner model).

5. **planner**
   - Emit a structured plan (JSON schema) with explicit tool invocations.
   - Must include bounds: max iterations, timeouts, expected checks.
   - Prefer “spec-first” outputs for code changes:
     - acceptance criteria
     - files to touch
     - checks to run

6. **executor**
   - Call ToolNode(s) for: file reads, searches, patch application, test/build.
   - Use deterministic tool invocation (no freeform shell without templates).
   - Attach tool provenance (inputs, outputs, timings) into state.

7. **verifier**
   - Run ruff/mypy/pytest (Python) and fmt/clippy/test (Rust).
   - If failures: summarize, feed back to `planner` with failure context.
   - Produce a machine-readable verification report (JSON) for CI parity.

8. **reporter**
   - Produce final response with exact file references and verification status.

### 3.3 Graph topology

- Primary loop: `planner → executor → verifier → (planner | reporter)`.
- Conditional edge: stop if verification passes and plan complete.
- Hard bounds:
  - `max_loops` (e.g., 3)
  - `max_tool_calls` per loop
  - `max_patch_bytes`

SOTA 2026 topology patterns (use as subgraphs):

- **Parallel context**: `context_builder` fans out to (repo summary, dependency scan, docs scan, symbol search) and joins.
- **Map-reduce retrieval**: retrieve per subsystem, then reduce to a single “fact pack”.
- **Two-stage planning**: router → draft plan → policy refinement → final plan.
- **Human-in-the-loop**: mutation plan must go through `approval_gate` before `apply_patch`.

### 3.4 Checkpointing and resumability

- Use LangGraph checkpointing to persist state after each node.
- Store:
  - plan
  - tool transcripts
  - diffs
  - verification artifacts

This enables:

- crash recovery
- audit logs
- offline evaluation

Recommended checkpoint keys:

- `job_id`, `run_id`, `graph_version`, `prompt_version`
- `repo_revision` (git SHA) + dirty diff hash
- `tool_runner_version`

## 4) Tooling Model

### 4.1 Tool categories

- **Read-only tools**: list files, read files, grep/search.
- **Mutation tools**: apply_patch only (no direct file writes), dependency updates.
- **Execution tools**: run `uv`, `pytest`, `ruff`, `mypy`, `cargo`.

Recommended additional tool types:

- **Diff inspectors**: validate patch semantics (path allowlist, forbidden patterns, license headers).
- **Repo analyzers**: parse manifests (`pyproject.toml`, `Cargo.toml`), lockfiles, workspace topology.
- **Doc generators**: update ADRs, changelog entries, and architecture docs.

### 4.2 Tool contracts

Tools must return:

- `ok`: bool
- `stdout` / `stderr`
- `exit_code`
- `artifacts`: structured outputs (diffs, reports)
- `timing_ms`

Avoid “stringly-typed” tool outputs: prefer JSON with stable schemas.

Define a single envelope schema for all tool results (recommended):

```json
{
  "tool": "cargo_test",
  "ok": true,
  "exit_code": 0,
  "stdout": "...",
  "stderr": "...",
  "timing_ms": 12345,
  "artifacts": {
    "reports": [],
    "diff": null
  }
}
```

### 4.3 Sandboxing

- Runner process runs with:
  - working directory allowlist
  - path traversal protection
  - network disabled by default
  - CPU/memory/time limits
  - command allowlist
- Store secrets only in runner env; orchestrator sees redacted views.

Practical file safety policies:

- Default deny: dotfiles that can exfiltrate or execute (`.ssh/`, `.git/config`, CI secrets).
- Default deny: binary files unless explicitly allowed.
- Guard “dependency churn”: require explicit approval to change lockfiles.

## 5) Retrieval (RAG) for Coding

Recommended retrieval mix:

- BM25 for exact symbol/file matches.
- Vector retrieval for semantic matches.
- Re-rank (small cross-encoder or LLM re-rank) for top 20 → top 5.

Chunking:

- Code: by symbol boundaries (functions, structs, modules)
- Docs: 500–1200 tokens with overlap

Always attach citations:

- file path
- line range
- commit hash (if available)

SOTA 2026 retrieval guidance:

- Prioritize **repo-native truth** (manifests, source, ADRs) over web retrieval.
- If using web retrieval, store fetched pages as immutable artifacts and cite them.
- Add a “freshness policy” per dependency and language (avoid stale patterns).

## 6) Multi-model Strategy

Use a tiered approach:

- Router: small, cheap model (intent classification + risk).
- Planner: stronger reasoning model.
- Coder: code-focused model.
- Verifier helper: small model to triage test failures.

Add fallbacks:

- provider A → provider B
- large → medium model when rate-limited

Add caching:

- prompt+context hash → completion
- tool output cache (read-only tools)

Model configuration should be explicit and versioned. Recommended keys:

- `provider`, `model`, `temperature`, `top_p`, `max_tokens`
- tool-call mode: strict JSON schema / function calling
- safety mode: refusal/guardrail thresholds
- prompt templates version (git hash)

## 7) Quality Gates (Python + Rust)

### 7.1 Python gates

- `ruff format`
- `ruff check --fix` (only if allowed)
- `mypy --strict`
- `pytest -q`

### 7.2 Rust gates

- `cargo fmt --check`
- `cargo clippy --all-targets --all-features -- -D warnings`
- `cargo test`

### 7.3 Patch discipline

- All mutations happen via patches.
- Patch is validated against:
  - allowed paths
  - max size
  - no binary modifications unless explicitly allowed

## 8) Suggested Repo Layout

```
.
├── py/                # Python orchestrator
├── rs/                # Rust runner
├── docs/
│   ├── langgraph_plan.md
│   └── adr/
├── prompts/           # versioned prompts + schemas
├── eval/              # regression tasks + fixtures
└── infra/             # docker/compose/k8s/ci
```

Recommended additions for “general coding” assistants:

- [`configs/`](configs/:1): runtime profiles (dev/stage/prod), policy allowlists, model routing tables
- [`schemas/`](schemas/:1): JSON schemas for planner output, tool envelopes, verifier reports
- [`scripts/`](scripts/:1): developer workflows (bootstrap, test, lint, eval)

## 9) Configuration: Concrete Defaults

### 9.1 Python: uv + ruff + mypy (strict)

- Use `uv` to lock dependencies and create reproducible environments.
- Pin LangGraph/LangChain family versions.
- Enable ruff’s formatting and import sorting.
- Run mypy in strict mode; prefer explicit types at graph boundaries.

Suggested baseline files (templates):

- [`py/pyproject.toml`](py/pyproject.toml:1): uv project, ruff, mypy, pytest, tool configs
- [`py/uv.lock`](py/uv.lock:1): generated lockfile (commit for reproducibility)
- [`py/ruff.toml`](py/ruff.toml:1) (optional): keep all Python tooling config in one place

Ruff + mypy “production defaults”:

- Enable ruff formatter; avoid black/isort duplication.
- Prefer `ruff check --output-format=github` in CI.
- mypy strict, but allow `--disable-error-code=type-abstract` only if necessary.

### 9.2 Rust: clippy pedantic

- Use `#![deny(clippy::pedantic)]` for the runner crate if feasible.
- Treat warnings as errors in CI.
- Use `tracing` spans on every tool invocation.

Suggested baseline files (templates):

- [`rs/Cargo.toml`](rs/Cargo.toml:1): workspace + runner crate(s)
- [`rs/rustfmt.toml`](rs/rustfmt.toml:1): formatting policy
- [`rs/.cargo/config.toml`](rs/.cargo/config.toml:1): build profiles, registry mirrors (optional)

## 10) Example LangGraph Skeleton (pseudocode)

Key constructs:

- `StateGraph` for defining nodes and edges.
- `ToolNode` for calling tools.
- `add_conditional_edges` for bounded loops.

Pseudo-structure:

1. Build graph with nodes: ingest, policy_gate, context_builder, router, planner, executor, verifier, reporter.
2. Add conditional edge from verifier:
   - pass → reporter
   - fail + loops remaining → planner
   - fail + loops exhausted → reporter (with failure summary)

## 11) Operational Checklist

- Deterministic tool runner with allowlists
- Checkpointing enabled
- Bounded autonomy and rate limits
- CI mirrors verifier gates
- Evaluation suite with golden tasks
- Observability (trace + metrics + structured logs)

## 12) “SOTA 2026” Configuration Blueprint (copy/paste ready)

This section describes *what to configure*, *where*, and *why* for teams building reliable AI coding/orchestration systems.

### 12.1 Runtime profiles

Use explicit profiles to avoid hidden behavior.

- `dev`: maximum logs, smaller models, permissive budgets, local SQLite checkpoints
- `stage`: mirrors prod, canary eval tasks on every deploy
- `prod`: strict budgets, approvals, audit retention, rate limiting

Recommended files:

- [`configs/runtime.dev.toml`](configs/runtime.dev.toml:1)
- [`configs/runtime.stage.toml`](configs/runtime.stage.toml:1)
- [`configs/runtime.prod.toml`](configs/runtime.prod.toml:1)

Minimum keys:

```toml
[models.router]
provider = "..."
model = "..."
temperature = 0.0

[models.planner]
provider = "..."
model = "..."
temperature = 0.2

[budgets]
max_loops = 3
max_tool_calls_per_loop = 12
max_patch_bytes = 200000
tool_timeout_s = 600

[policy]
network_default = "deny"
require_approval_for_mutations = true
```

### 12.2 Tool allowlists and command templates

Define tools as **named templates**. Do not allow arbitrary shell.

Example allowlist policy:

```toml
[tools]
read_file = { allowed = true }
search_files = { allowed = true }
apply_patch = { allowed = true, approval_required = true }

[tools.exec]
uv = { allowed = true, args_allowlist = ["sync", "run"] }
pytest = { allowed = true, args_allowlist = ["-q"] }
ruff = { allowed = true, args_allowlist = ["check", "format"] }
mypy = { allowed = true }
cargo = { allowed = true, args_allowlist = ["fmt", "clippy", "test"] }
```

Runner enforcement belongs in Rust (server-side); orchestrator enforces client-side too.

### 12.3 Prompt and schema versioning

Treat prompts like code:

- Put all prompts under [`prompts/`](prompts/:1)
- Put JSON schemas under [`schemas/`](schemas/:1)
- Reference both by git SHA in every job record

Planner output schema (minimum):

- `steps[]`: `{ id, description, tools[], expected_outcome, files_touched[] }`
- `verification[]`: commands to run
- `rollback`: how to revert

### 12.4 Evaluation suite (offline + online)

The difference between “demo” and “production” in 2026 is evaluation discipline.

Create an `eval/` harness that runs:

- deterministic tasks (unit)
- repo-specific tasks (integration)
- adversarial tasks (security)

Metrics to track:

- build pass rate after first patch
- mean tool calls per task
- time-to-green
- hallucination rate (measured via citation checks)
- policy violations (attempted forbidden tools/paths)

Suggested layout:

- [`eval/tasks/`](eval/tasks/:1)
- [`eval/fixtures/`](eval/fixtures/:1)
- [`eval/golden/`](eval/golden/:1)

### 12.5 Observability

Minimum viable observability:

- trace per job (OpenTelemetry)
- spans per node and tool call
- log correlation: `job_id`, `run_id`, `trace_id`
- redaction: ensure secrets never enter logs

### 12.6 Security, governance, and compliance defaults

Recommended default posture:

- no network unless explicitly enabled per tool
- read-only by default; explicit approval to mutate
- secrets only in runner; orchestrator never prints secrets
- path allowlist + denylist
- immutable audit artifacts (tool transcripts, diffs, verification reports)

### 12.7 Developer ergonomics (fast local loops)

Provide a single entrypoint script per language:

- [`scripts/dev.cmd`](scripts/dev.cmd:1) (Windows)
- [`scripts/dev.ps1`](scripts/dev.ps1:1) (PowerShell)
- [`scripts/dev.sh`](scripts/dev.sh:1) (bash)

Each should do:

- bootstrap (uv sync / cargo fetch)
- lint + typecheck
- unit tests
- run eval smoke set

### 12.8 CI configuration

CI should mirror verifier gates and also run a small eval canary.

Recommended files:

- [`infra/ci/github-actions.yml`](infra/ci/github-actions.yml:1) or equivalent

CI stages:

1) Python: `ruff format --check`, `ruff check`, `mypy --strict`, `pytest`
2) Rust: `cargo fmt --check`, `cargo clippy ... -D warnings`, `cargo test`
3) Eval: `python -m eval.run --suite canary`
4) SBOM + dependency audit (optional)

## 13) Minimal “use case mapping” for your request

Your request (“general coding using Python, Rust, frameworks, GEN AI, tooling and scripting”) maps cleanly to the following LangGraph subgraphs:

- **Code change subgraph**: context → plan(spec) → apply_patch → verify → report
- **Research subgraph**: query expand → retrieve (repo + web if allowed) → synthesize → cite
- **Tooling subgraph**: environment scan → propose config → generate patches → verify

The configuration blueprint in section 12 is the operational glue that makes these subgraphs safe and repeatable.

