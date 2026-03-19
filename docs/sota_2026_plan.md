# SOTA 2026 Plan: Focused Adaptation Plan

This document updates the earlier roadmap using the current repository state rather than an aspirational baseline. The codebase already contains several 2026-aligned building blocks, so the next step is not a rewrite; it is a focused maturation pass across orchestration, routing, compression, MCP integration, and telemetry.

## 1. Current baseline after code inspection

The repository is ahead of where the previous version of this document assumed it was.

### Already present

- **A bounded verify/retry loop already exists.** The graph in `py/src/lg_orch/graph.py` is not purely linear anymore. `verifier` can route back through `policy_gate`, and `policy_gate` can send execution back to `planner` or `context_builder`.
- **Loop-budget enforcement already exists.** `py/src/lg_orch/nodes/policy_gate.py` enforces `max_loops` and emits a structured verification-style failure when the loop budget is exhausted.
- **Context reset and architecture-mismatch recovery already exist in minimal form.** `py/src/lg_orch/nodes/verifier.py` can classify failures as `context_builder` retries and request plan discard/reset.
- **Repository structure and retrieval support already exist.** `py/src/lg_orch/nodes/context_builder.py` generates a repo map and can pull runner-backed AST summaries plus semantic hits.
- **Basic history pruning already exists.** `py/src/lg_orch/memory.py` performs sliding-window pruning and post-verification read payload eviction.
- **Basic model routing telemetry already exists.** `py/src/lg_orch/model_routing.py` records per-node model-route decisions.
- **MCP tool discovery/execution already exists in the runner.** `rs/runner/src/tools/mcp.rs` supports `initialize`, `tools/list`, and `tools/call`, and `py/src/lg_orch/tools/mcp_client.py` wraps discovery/execution on the Python side.
- **Checkpointing, snapshots, undo metadata, and run traces already exist.** These appear in state/config, runner envelopes, and trace writing.

### Important correction to the old plan

The previous version of this document said the graph was still `planner -> executor -> verifier -> reporter`. That is now outdated. The real gap is not the absence of a loop; it is that the existing loop is still shallow, lightly typed, and missing stronger recovery semantics.

## 2. Gap analysis versus the requested 2026 themes

| Theme | Present now | Concrete gap | Why it matters |
| --- | --- | --- | --- |
| Autonomous plan/execute/verify/recover loops | Graph loop, retry target, context reset, max-loop budget | No explicit recover contract, no failure fingerprint, no loop summary/fact pack, and `plan.verification` is not being executed as a first-class phase | The loop exists, but it is still closer to retry-on-failure than to deliberate recovery |
| Heterogeneous routing | Basic local-vs-remote routing plus task-class fallback | No real router node, no capability matrix, no cost/latency/affinity-aware routing, no per-lane policy for planner vs verifier vs summarizer | The system cannot yet adapt model choice to task difficulty, context size, or cache locality |
| Algorithmic context compression | Repo map, AST summary, semantic hits, history pruning | No token budget, no stable-prefix vs ephemeral-context split, no map-reduce fact pack, no salience scoring, no compression telemetry | Context is gathered, but not strategically compressed for long-running loops |
| MCP sampling/protocol readiness | `initialize`, `tools/list`, `tools/call`, redaction metadata, Python MCP wrapper | MCP tools are not injected into planning context, and support for `resources`, `prompts`, `roots`, and `sampling` is absent | The runner can speak a useful subset of MCP, but the orchestrator is not yet truly MCP-native |
| llm-d style prefix-cache telemetry/affinity | Basic trace events, model-route logs, tool timings | No prompt-prefix segmentation, no cache key/candidate key, no cache hit/miss telemetry, no affinity tags, no provider usage metrics surfaced back into orchestration state | There is no way to optimize prompt locality or observe cache behavior across retries |
| Streaming inference | No streaming path in inference client or runner HTTP layer | All LLM calls are blocking request/response; any context >4 k tokens creates a synchronous wait that blocks the entire graph step | Competitive products all stream; this is the single largest perceived latency gap |
| Concurrent tool execution | `batch_execute_tool` in `rs/runner/src/main.rs` iterates calls in a serial `for` loop | Tool batches that could run in parallel (e.g. parallel file reads + search) pay full sequential latency | A `tokio::JoinSet` fan-out is a one-file change with high leverage |
| Editor integration surface | `vscode-extension/src/extension.ts` is a stub; no tree view, diff preview, or approval UI | The platform has no viable distribution channel until the extension is functional | This is the primary user-facing gap versus Aider / continue.dev / Cursor |

## 3. Focused findings by subsystem

### 3.1 Orchestration loop

- `py/src/lg_orch/graph.py` already supports retry routing, but recovery is implicit rather than explicit.
- `py/src/lg_orch/nodes/verifier.py` currently **classifies** failed tool results; it does not own an independent verification execution pass beyond what was already run in the plan.
- `schemas/planner_output.schema.json` and `py/src/lg_orch/state.py` still model planning as a simple list of steps plus a verification list, without recovery metadata, iteration intent, or completion criteria.
- `schemas/verifier_report.schema.json` does not capture failure fingerprints, recovery reasons, context scope hints, or verifier-generated next actions.

### 3.2 Routing

- `py/src/lg_orch/model_routing.py` only makes a binary `local`/`remote` decision with a task-class fallback list.
- `prompts/router.md` exists, but there is no router node in `py/src/lg_orch/graph.py` and no orchestrator stage that consumes the prompt.
- `py/src/lg_orch/tools/inference_client.py` returns only model text, so routing decisions cannot learn from latency, usage, or provider-side cache signals.

### 3.3 Context compression

- `py/src/lg_orch/nodes/context_builder.py` is a good baseline: repo map, AST summary, and semantic hits are useful building blocks.
- `py/src/lg_orch/memory.py` prunes by count and character length, not by token budget, salience, or recoverability value.
- There is no explicit "stable prefix" layer containing durable repo facts versus an "ephemeral working set" layer containing loop-local evidence.

### 3.4 MCP readiness

- `rs/runner/src/tools/mcp.rs` is already a serious step toward MCP readiness, including a proper JSON-RPC handshake and redaction-aware envelopes.
- `py/src/lg_orch/tools/mcp_client.py` is not wired into `context_builder`, `planner`, or `executor` flow, so discovered tools do not affect plans.
- The current subset is tool-centric. It is not yet ready for the broader MCP 2026 shape that includes resources, prompts, roots, and sampling.

### 3.5 Telemetry, diagnostics, and cache affinity

- `rs/runner/src/diagnostics.rs` parses compiler/runtime diagnostics into a normalized structure, which is valuable.
- The diagnostics layer still lacks higher-level failure clustering/fingerprinting that the recover loop could use.
- `py/src/lg_orch/trace.py` records events, but not rich spans, usage counters, cache metadata, or affinity tags.
- `configs/runtime.dev.toml` configures models and budgets, but not compression budgets, route lanes, cache affinity, or telemetry toggles for model metadata capture.

## 4. Pragmatic target design

The right target is not "full SOTA everywhere." It is a narrow set of changes that make the current architecture materially more autonomous and observable without turning the repository into an experimental multi-agent platform.

### 4.1 Implement now

#### A. Tighten the plan/execute/verify/recover contract

Implement these changes now:

1. **Make recovery explicit in the data model.**
   - Extend planner and verifier schemas so the loop carries a structured recovery packet: failure class, failure fingerprint, retry rationale, next context scope, and whether the plan should be amended or discarded.
2. **Make verification a real execution phase.**
   - The verifier should execute the structured verification calls from the plan rather than only inspect previously failed tool results.
3. **Add completion criteria and per-loop accounting.**
   - Every plan should carry acceptance criteria, maximum allowed iterations, and expected verification checks.
4. **Enforce the budgets that already exist in config.**
   - `max_tool_calls_per_loop` and `max_patch_bytes` are configured but not yet enforced in the main loop.
5. **Persist loop summaries.**
   - After each failed loop, store a compressed "what changed / what failed / what to try next" fact pack for the next planner pass.

#### B. Add real heterogeneous routing

Implement these changes now:

1. **Add a router node before planning.**
   - Use `prompts/router.md` as an actual routing contract.
2. **Route by lane, not only by provider.**
   - Decide among at least `interactive`, `deep_planning`, and `recovery` lanes.
3. **Upgrade model routing from fallback rules to capability rules.**
   - Routing should consider task class, context size, retry stage, latency sensitivity, and cache affinity tag.
4. **Record routing telemetry in state.**
   - Include lane, provider, model, rationale, latency, and cache-affinity label.

#### C. Add algorithmic context compression

Implement these changes now:

1. **Split context into stable and ephemeral layers.**
   - Stable prefix: repo summary, AST summary, selected docs, discovered MCP catalog.
   - Ephemeral working set: latest failures, current plan, latest tool outputs, loop summary.
2. **Add a compression policy with budgets.**
   - Use token or approximate-token budgets rather than only character thresholds.
3. **Summarize before evicting.**
   - Large tool outputs should become compact summaries with provenance, not only hashes.
4. **Track compression decisions in telemetry/provenance.**
   - The next planner pass should know what was compressed away and why.

#### D. Make MCP planner-visible and telemetry-aware

Implement these changes now:

1. **Surface discovered MCP tools into planning context when MCP is enabled.**
2. **Store MCP capability metadata in the repo/context layer.**
3. **Capture model/provider response metadata in inference telemetry.**
   - Tokens when available.
   - Request latency.
   - Provider/model identifiers.
   - Cache-related headers/metadata when available.
4. **Add normalized failure fingerprints in diagnostics.**
   - This gives the recover loop a stable signal instead of only raw stderr summaries.

### 4.2 Keep future-facing for documentation only

These items should remain documented as future-facing rather than immediate implementation work:

- **Full MCP surface area:** `resources/list`, `resources/read`, `prompts/list`, `prompts/get`, `roots`, and `sampling`.
- **Persistent MCP session pooling and multiplexing** across long-lived orchestrator runs.
- **True llm-d distributed prefix-cache scheduling** across replicas, with locality-aware worker placement and shared cache registries.
- **Full vector database + re-ranker stack** beyond the existing local semantic search and AST summary path.
- **Browser/web automation** and visual validation tools.
- **Parallel multi-agent branches** for speculative planning or multi-file repair.

Those are real 2026 patterns, but they are not the next correct move for this repository.

## 5. Short execution roadmap before coding

This section replaces the older implementation order below it. The older version assumed missing routing and loop structure that now already exist in the codebase.

---

## Wave 0 — Security Hardening (Priority 0 — Prerequisite for any multi-tenant deployment)

**Goal:** Eliminate the approval token forgery vulnerability before any production or multi-tenant deployment. This wave has no dependencies and must be completed before Wave 10 RBAC work is meaningful.

**Status:** PLANNED

### Security gap: unsigned approval tokens

[`rs/runner/src/approval.rs`](../rs/runner/src/approval.rs:58) currently produces tokens using `format!("approve:{challenge_id}")` — a plain string with no cryptographic signature, no server secret, no expiry, and no nonce. Any party that can observe a token in transit can forge one for a different `challenge_id`.

**Required changes:**

- **HMAC-SHA256 signed tokens** — replace `format!("approve:{challenge_id}")` in [`rs/runner/src/approval.rs`](../rs/runner/src/approval.rs) with `hmac_sha256(secret, challenge_id + ":" + iat + ":" + exp)` where `secret` is loaded from `LG_RUNNER_APPROVAL_SECRET` env var at startup; abort if env var is absent in non-dev mode.
- **`iat`/`exp` claims** — embed issued-at and expiry timestamps (ISO 8601 or Unix epoch) in the token payload before signing. Default expiry: 15 minutes. Tokens presented after expiry must be rejected with `401 Expired`.
- **Nonce** — add a random 128-bit nonce to the signed payload to prevent replay attacks.
- **Token verification in approval handler** — update the `POST /approve` handler to re-derive the HMAC, compare in constant time, and check expiry before accepting the approval action.
- **Secret rotation support** — support a `LG_RUNNER_APPROVAL_SECRET_PREVIOUS` env var for zero-downtime key rotation; accept tokens signed by either current or previous secret during the rotation window.
- **Config guard** — [`rs/runner/src/config.rs`](../rs/runner/src/config.rs) must expose an `approval_secret_required: bool` flag defaulting to `true`; setting it `false` must emit a `WARN` log and is only valid in `dev` profile.
- **Tests** — add property-based tests (`proptest`) verifying that a token generated for `challenge_id=A` is rejected for `challenge_id=B`, and that expired tokens are rejected.

---

### Wave 1: documentation sync and shipping baseline

Targets:

- [`README.md`](../README.md)
- [`docs/architecture.md`](architecture.md)
- [`docs/platform_console.md`](platform_console.md)
- [`docs/sota_2026_plan.md`](sota_2026_plan.md)

Status: **Completed**. Docs have been aligned with the actual graph, router, trace, and checkpoint behavior.

### Wave 2: first usable product surface

Targets:

- [`py/src/lg_orch/main.py`](../py/src/lg_orch/main.py)
- [`py/src/lg_orch/trace.py`](../py/src/lg_orch/trace.py)
- [`py/src/lg_orch/visualize.py`](../py/src/lg_orch/visualize.py)
- new minimal UI/API files

Status: **Completed**. The existing graph export and trace artifacts have been packaged into a first usable run viewer (SPA + Trace Site). The UI supports displaying the graph, timeline, final output, and tool results.

### Wave 3: run API and persistence

Targets:

- [`py/src/lg_orch/main.py`](../py/src/lg_orch/main.py)
- [`py/src/lg_orch/checkpointing.py`](../py/src/lg_orch/checkpointing.py)
- [`py/src/lg_orch/trace.py`](../py/src/lg_orch/trace.py)
- [`py/src/lg_orch/remote_api.py`](../py/src/lg_orch/remote_api.py)
- [`py/src/lg_orch/run_store.py`](../py/src/lg_orch/run_store.py)

Goal:

- Add run listing, run detail, replay metadata, and durable storage.
- Prefer SQLite first; leave Postgres and multi-user concerns for later.

Status: **Completed**. The repository now has a usable HTTP run API with run listing, run detail, cancellation, trace-backed run detail views, and durable SQLite-backed storage for run metadata and recovery facts.

### Wave 4: provider expansion and routing maturity

Targets:

- [`py/src/lg_orch/config.py`](../py/src/lg_orch/config.py)
- [`py/src/lg_orch/tools/inference_client.py`](../py/src/lg_orch/tools/inference_client.py)
- [`py/src/lg_orch/model_routing.py`](../py/src/lg_orch/model_routing.py)
- [`configs/runtime.dev.toml`](../configs/runtime.dev.toml)

Goal:

- Keep DigitalOcean support intact.
- Add another OpenAI-compatible provider path cleanly so platform choice is config-driven rather than hard-coded.
- Surface provider latency, usage, and cache-related telemetry into routing decisions.

Status: **Completed**. DigitalOcean and OpenAI-compatible providers are both wired through config-driven runtime settings, routing decisions are lane-aware, and inference telemetry now records provider/model identity, latency, usage, and cache-related metadata.

### Wave 5: parity-focused agent quality work

Targets:

- [`py/src/lg_orch/nodes/context_builder.py`](../py/src/lg_orch/nodes/context_builder.py)
- [`py/src/lg_orch/nodes/planner.py`](../py/src/lg_orch/nodes/planner.py)
- [`py/src/lg_orch/nodes/executor.py`](../py/src/lg_orch/nodes/executor.py)
- [`py/src/lg_orch/nodes/verifier.py`](../py/src/lg_orch/nodes/verifier.py)
- [`py/src/lg_orch/memory.py`](../py/src/lg_orch/memory.py)
- [`eval/run.py`](../eval/run.py)
- [`eval/tasks/canary.json`](../eval/tasks/canary.json)

Goal:

- Improve recovery quality, context compression, and evaluation discipline.
- Measure progress against repeatable tasks instead of relying on anecdotal demos.

Status: **Completed**. The current implementation carries recovery packets and loop summaries through planning and verification, builds stable-prefix and working-set context layers with compression provenance, recalls episodic recovery facts from durable storage, and measures these behaviors in the eval suite and canary task.

### Wave 6: execution quality, streaming, and distribution

Targets:

- [`rs/runner/src/main.rs`](../rs/runner/src/main.rs)
- [`py/src/lg_orch/tools/inference_client.py`](../py/src/lg_orch/tools/inference_client.py)
- [`vscode-extension/src/extension.ts`](../vscode-extension/src/extension.ts)
- [`eval/run.py`](../eval/run.py)
- new `eval/tasks/real_world_repair.json` (curated 10-task bug-fix benchmark)

Goal:

1. **Concurrent batch execution in the Rust runner.** Replace the serial `for` loop in `batch_execute_tool` with a `tokio::JoinSet` fan-out. This is a single-file change that removes the primary throughput bottleneck for multi-tool plans.
2. **Streaming inference wired into the interactive lane.** `InferenceClient.chat_completion_stream()` is already implemented. Wire the `interactive` lane nodes in `planner.py` and `router.py` to the stream path so partial tokens surface progressively instead of blocking the graph step. Required for perceived latency parity with Aider and Claude Code.
3. **VSCode extension activation.** Wire `vscode-extension/src/extension.ts` to at least: display current run status, show the last verifier report, and surface approval prompts for mutation plans. Without this, the platform has no viable distribution channel.
4. **Outcome quality benchmark.** Add a curated set of 10 known-good bug-fix tasks with `expected_patch` assertions. Pass rate (not only structural behavior) is what enables real parity comparisons. The current eval suite tests routing and loop mechanics but does not measure whether the agent produces correct code.

Status: **Completed.** The repository now has concurrent runner fan-out, streaming inference in interactive router/planner paths, an activated VS Code extension, and the real-world repair benchmark.

### Wave 7: SOTA platform UX/UI

Without an immersive, polished, and accessible interface, the most sophisticated agentic architecture remains a difficult tool used only by specialists. Competitive parity in 2026 requires not just technical depth but a product-quality user experience that rivals linear-editing tools like Cursor and Windsurf.

Targets:

- [`py/src/lg_orch/visualize.py`](../py/src/lg_orch/visualize.py)
- [`py/src/lg_orch/remote_api.py`](../py/src/lg_orch/remote_api.py)
- [`vscode-extension/src/extension.ts`](../vscode-extension/src/extension.ts)
- New `py/src/lg_orch/spa/` — standalone SPA frontend

Goal:

1. ✅ **Live run console with streaming timeline.** Live SSE streaming console implemented in [`py/src/lg_orch/spa/`](../py/src/lg_orch/spa/) with real-time event streaming. SSE endpoint added to [`py/src/lg_orch/remote_api.py`](../py/src/lg_orch/remote_api.py) (`GET /runs/{id}/stream`). Static SPA serving infrastructure and event streaming architecture fully documented in [`docs/wave7_spa_sse.md`](wave7_spa_sse.md).

2. ✅ **Agent activity visualization.** D3 v7 force-directed graph implemented in [`py/src/lg_orch/spa/main.js`](../py/src/lg_orch/spa/main.js) and [`py/src/lg_orch/spa/index.html`](../py/src/lg_orch/spa/index.html). Library loaded from CDN (`https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js`). Nodes carry `idle`/`active`/`done`/`error` states driven by SSE events. The retry edge (`verifier → policy_gate`) renders as a dashed orange line. Active nodes pulse via an SVG `feGaussianBlur` glow filter. Layout is responsive via `ResizeObserver`.

3. ✅ **Verifier report panel with inline diffs.** GitHub-style unified diff panel with syntax highlighting is complete. Approval buttons for gated exec calls appear inline in the activity stream.

4. ✅ **Run history and search.** Persistent run history is implemented. SQLite FTS5 full-text search is now backed by the `runs_fts` virtual table in [`py/src/lg_orch/run_store.py`](../py/src/lg_orch/run_store.py) (`search_runs(query, limit)`), with INSERT/UPDATE/DELETE triggers keeping the index in sync; falls back to a LIKE scan when FTS5 is unavailable. Search is exposed via [`py/src/lg_orch/remote_api.py`](../py/src/lg_orch/remote_api.py) as `GET /runs/search?q=<query>&limit=<n>` returning `{"results": [...], "total": N}`.

5. ✅ **Responsive, design-system-quality typography and layout.** Modern developer tool design principles applied with clean monospace code blocks, semantic color coding for success/failure/warning, and responsive layout served from the Python API's static asset layer.

6. ✅ **VSCode extension premium UX.** New files [`vscode-extension/src/RunTreeProvider.ts`](../vscode-extension/src/RunTreeProvider.ts) and [`vscode-extension/src/RunPanelProvider.ts`](../vscode-extension/src/RunPanelProvider.ts) implement the run tree view and webview panel respectively. [`vscode-extension/src/extension.ts`](../vscode-extension/src/extension.ts) registers commands `orchestrator.refreshRuns`, `orchestrator.openRun`, and `orchestrator.newRun`. [`vscode-extension/package.json`](../vscode-extension/package.json) contributes the `orchestratorRuns` tree view in the activity bar and all three command menu entries.

Design references:
- [Vercel AI Playground](https://sdk.vercel.ai) — streaming token visualization
- [Replit Ghostwriter Agent](https://replit.com/ai) — live agent trace with node graph
- [Cursor composer](https://cursor.sh) — multi-file diff approval flow
- [Linear](https://linear.app) — motion design and transition polish
- VS Code [Webview API](https://code.visualstudio.com/api/extension-guides/webview) — native editor UX patterns

Status: **COMPLETE.** All six goals delivered. Live SSE streaming console, static SPA serving, and event streaming architecture are fully implemented. D3 v7 force-directed agent graph with node-state animations and responsive resize is complete. SQLite FTS5 full-text run search with HTTP endpoint is complete. VS Code extension `RunTreeProvider`, `RunPanelProvider`, and command handlers are complete. All tests pass.

### Wave 8: collaborative agents and governed autonomy

The next wave should combine the two highest-leverage findings from the current repository state:

1. Lula still behaves primarily as a strong staged specialist pipeline rather than a fully collaborative agent system.
2. Lula can detect approval-required execution states, but it still lacks the full governed execution loop needed for enterprise-grade operator control.

This wave should therefore pair **contract-first collaborative agents** with a **governed-autonomy control plane** rather than treating them as competing directions.

Targets:

- [`py/src/lg_orch/graph.py`](../py/src/lg_orch/graph.py)
- [`py/src/lg_orch/state.py`](../py/src/lg_orch/state.py)
- new `py/src/lg_orch/nodes/coder.py`
- [`py/src/lg_orch/nodes/planner.py`](../py/src/lg_orch/nodes/planner.py)
- [`py/src/lg_orch/nodes/router.py`](../py/src/lg_orch/nodes/router.py)
- [`py/src/lg_orch/nodes/verifier.py`](../py/src/lg_orch/nodes/verifier.py)
- [`py/src/lg_orch/remote_api.py`](../py/src/lg_orch/remote_api.py)
- [`py/src/lg_orch/run_store.py`](../py/src/lg_orch/run_store.py)
- [`py/src/lg_orch/trace.py`](../py/src/lg_orch/trace.py)
- [`py/src/lg_orch/visualize.py`](../py/src/lg_orch/visualize.py)
- [`vscode-extension/src/extension.ts`](../vscode-extension/src/extension.ts)
- [`schemas/planner_output.schema.json`](../schemas/planner_output.schema.json)
- [`schemas/verifier_report.schema.json`](../schemas/verifier_report.schema.json)
- [`eval/tasks/`](../eval/tasks/)
- new [`py/src/lg_orch/meta_graph.py`](../py/src/lg_orch/meta_graph.py) — dependency-aware multi-agent scheduler

Goal:

#### A. Contract-first collaborative agents

1. ✅ **Add an explicit coder specialist.**
   - Coder node inserted between planner and executor; patch synthesis is now an explicit specialist phase.
2. ✅ **Introduce a uniform handoff envelope.**
   - Planner, coder, verifier, and recovery paths exchange structured artifacts containing objective, file scope, evidence, constraints, acceptance checks, retry budget, and provenance.
3. ✅ **Retune the router into a topology selector.**
   - Router decides lane, model tier, and execution topology (planner-only, planner-to-coder, verifier-driven repair).
4. ✅ **Make verifier-directed retries specialist-aware.**
   - Localized implementation failures route to coder; broader contract or architecture failures route back to planner or context rebuilding.
5. ✅ **Keep execution deterministic.**
   - Rust runner boundary preserved as core design strength; no free-form LLM execution.

#### B. Governed-autonomy control plane

1. ✅ **Approval API.**
   - Approve/reject endpoints added; pending operations actionable through API.
2. ✅ **Suspend/resume orchestration.**
   - Approval-required execution is a first-class suspended run state with checkpoint-based resume.
3. ✅ **Durable approval state and audit trail.**
   - Approver identity, challenge id, timestamps, operation class, and affected paths persisted in run store and trace artifacts.
4. ✅ **Client wiring.**
   - SPA and VS Code extension approval buttons backed by real API actions.
5. ✅ **Eval coverage.**
   - Approval-path tasks added covering block-before-approval, resume-after-approval, reject termination, and audit metadata preservation.

#### C. Dependency-aware multi-agent scheduler

1. ✅ **Meta-graph scheduler implementation.**
   - [`py/src/lg_orch/meta_graph.py`](../py/src/lg_orch/meta_graph.py) fully rewritten with `DependencyGraph`, `MetaGraphScheduler`, cycle detection, fail-fast dependency handling, max-parallel concurrency cap, and comprehensive test coverage.
2. ✅ **MCP tool catalog wiring.**
   - `state["mcp_tools"]` field added; [`py/src/lg_orch/nodes/context_builder.py`](../py/src/lg_orch/nodes/context_builder.py) discovers MCP tools; [`py/src/lg_orch/nodes/planner.py`](../py/src/lg_orch/nodes/planner.py) injects tool catalog into planning prompts.
3. ✅ **Snapshot support.**
   - Snapshot infrastructure wired through policy gate, runner client, and approval flows.

Roadmap before coding:

1. ✅ **Slice 1 — contracts first.**
   - State and schemas extended with handoff envelopes and coder-facing task contracts.
2. ✅ **Slice 2 — explicit coder loop.**
   - Coder node added, planner-to-coder-to-executor flow wired, verifier retry routing updated.
3. ✅ **Slice 3 — governed execution.**
   - Approval endpoints, suspended run state, durable approval persistence, and resume logic complete.
4. ✅ **Slice 4 — client surfaces and evals.**
   - SPA and VS Code controls wired to approval API; control-flow paths covered in eval tasks and tests.

Expected outcome:

- Lula becomes a clearer multi-role system inside a single run: router, planner, coder, deterministic executor, verifier, reporter.
- Risky actions become observable, pausable, resumable, and auditable across API, SPA, and VS Code.
- The platform moves closer to enterprise-grade parity without prematurely expanding into uncontrolled multi-agent behavior.

#### D. File-lease / git-worktree branch isolation (now complete)

- ✅ **Worktree isolation module.** New [`py/src/lg_orch/worktree.py`](../py/src/lg_orch/worktree.py) provides `WorktreeContext`, `WorktreeError`, `create_worktree()`, `remove_worktree()`, `merge_worktree()`, and the `WorktreeLease` async context manager. `MetaGraphScheduler` gained a `worktree_isolation: bool` flag; when enabled, each sub-agent runs in its own `lg-orch/{run_id}` git branch and worktree and the branch is merged back on success. `OrchState` gained a `worktree_path: str | None` field.

#### E. Multi-path approval policies (now complete)

- ✅ **Approval policy module.** New [`py/src/lg_orch/approval_policy.py`](../py/src/lg_orch/approval_policy.py) implements `TimedApprovalPolicy` (auto-approve or auto-reject after a deadline), `QuorumApprovalPolicy` (majority of named approvers required), and `RoleApprovalPolicy` (at least one member of a named role required), plus `ApprovalVote`, `ApprovalDecision`, and `ApprovalEngine.evaluate()`. [`py/src/lg_orch/remote_api.py`](../py/src/lg_orch/remote_api.py) gained `POST /runs/{run_id}/approval-policy` and `POST /runs/{run_id}/vote`. [`py/src/lg_orch/state.py`](../py/src/lg_orch/state.py) gained `ApprovalRecord` with `policy` and `votes` fields.

#### F. Dynamic dependency resolution (now complete)

- ✅ **Runtime DAG rewiring.** [`py/src/lg_orch/meta_graph.py`](../py/src/lg_orch/meta_graph.py) `DependencyGraph` gained `add_edge()`, `remove_edge()`, and `clone()`. New `DependencyPatch` dataclass (`add_edges`, `remove_edges`) allows a completed agent to return `{"dependency_patch": DependencyPatch(...)}` to re-wire the remaining DAG at runtime. Patches are cycle-checked, applied to a sandbox clone, and atomically swapped in. `MetaGraphScheduler` gained a `dynamic_rewiring: bool` flag that gates this behavior.

Status: **COMPLETE.** All deliverables across slices 1–4 and the three additional Wave 8 extensions are complete: planner → coder flow, typed handoff envelopes, approval suspend/resume, snapshot support, meta-graph scheduler with dependency-aware execution, MCP tool catalog wiring, eval task coverage, git-worktree branch isolation, multi-path approval policies (timed, quorum, role-based), and dynamic dependency resolution. All tests pass.

## Infrastructure Completions (2026-03)

The following infrastructure gaps identified in the roadmap have been filled:

### Rust Project Configuration
- ✅ [`rs/rustfmt.toml`](../rs/rustfmt.toml) — Project formatting policy (100 char width, imports grouped std/external/crate, trailing commas)
- ✅ [`rs/.cargo/config.toml`](../rs/.cargo/config.toml) — Build profiles (dev, release, ci), Clippy aliases, env defaults

### CI Pipeline
- ✅ [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) — 5-job GitHub Actions pipeline:
  1. `lint-python` — ruff check + format + mypy strict
  2. `test-python` — pytest with coverage
  3. `lint-rust` — cargo fmt + clippy -D warnings
  4. `test-rust` — cargo test --all-features
  5. `eval-canary` — smoke test with dry-run mode

### Eval Infrastructure
- ✅ [`eval/fixtures/`](../eval/fixtures/) — Deterministic test input fixtures:
  - `canary/` — minimal Python file for routing smoke test
  - `test-repair/` — broken test scenario (calculator module)
  - `real-world-repair/` — latent bug scenario (zero-division handler)
  - `approval-flow/` — production deployment requiring approval
- ✅ [`eval/golden/`](../eval/golden/) — Expected output assertions for pass-rate benchmarking:
  - Golden files for canary, test-repair, real-world-repair, approval-suspend-resume
  - Assertion operators: `eq`, `lte`, `gte`, `in`, `contains`
  - README documenting assertion schema and eval runner integration

## 6. Practical file order

If implementation starts immediately, the highest-leverage order is:

1. [`docs/architecture.md`](architecture.md)
2. [`docs/platform_console.md`](platform_console.md)
3. [`README.md`](../README.md)
4. UI/API entry files to be introduced
5. [`py/src/lg_orch/trace.py`](../py/src/lg_orch/trace.py)
6. [`py/src/lg_orch/visualize.py`](../py/src/lg_orch/visualize.py)
7. [`py/src/lg_orch/checkpointing.py`](../py/src/lg_orch/checkpointing.py)
8. [`py/src/lg_orch/config.py`](../py/src/lg_orch/config.py)
9. [`py/src/lg_orch/tools/inference_client.py`](../py/src/lg_orch/tools/inference_client.py)
10. [`py/src/lg_orch/model_routing.py`](../py/src/lg_orch/model_routing.py)
11. [`py/src/lg_orch/nodes/context_builder.py`](../py/src/lg_orch/nodes/context_builder.py)
12. [`py/src/lg_orch/nodes/planner.py`](../py/src/lg_orch/nodes/planner.py)
13. [`py/src/lg_orch/nodes/verifier.py`](../py/src/lg_orch/nodes/verifier.py)
14. [`eval/run.py`](../eval/run.py)

## 7. Tradeoffs and risks

- UI first gives the fastest visible progress, but it can hide missing API and storage boundaries.
- Persistence first is cleaner architecture, but it delays the visible product jump.
- OpenAI-compatible provider expansion is low risk; non-compatible provider support is adapter work and can widen the maintenance surface.
- Competitive parity will come more from evaluation quality, recovery behavior, and UX polish than from adding one more model endpoint.

## 8. Summary

The repository is already past the "toy scaffold" stage. The next correct move is not to rebuild core orchestration; it is to package what already works into a usable product surface, add durable run state, make provider choice more portable, and then tighten recovery/evaluation quality until the system is visibly competitive.

## 9. Next-generation architecture pillars (from field research)

The following five innovations are drawn from a structured analysis of the 2026 agentic coding tool market. They represent the architectural gap between this platform's current state and true enterprise-grade autonomous software engineering. Each pillar is documented here as a forward-looking target; none requires immediate implementation, but all should inform the evolution of the platform beyond Wave 6.

### 9.1 Neurosymbolic vericoding

Current state-of-the-art agents — including this platform — rely entirely on probabilistic LLM output. Code is generated but never formally proven. This creates a "pull request review bottleneck": AI-generated code still requires expensive human or AI-on-AI review because correctness cannot be guaranteed.

**Vericoding** separates the connectionist generation phase from a symbolic verification phase. The LLM generates both executable code and a mathematical proof that the code satisfies a formal specification. A deterministic proof checker (e.g. Dafny, Verus for Rust, or Lean) either accepts or rejects the proof and returns exact failure points. The LLM ingests that deterministic feedback to repair the proof or implementation, looping until the checker passes.

Benchmark success rates (2026): Dafny 82%, Verus/Rust 44%, Lean 27%. The Rust runner in this platform is structurally well-positioned to integrate Verus since it already uses Rust as a first-class language.

**Relevance to this codebase:** The verifier node currently inspects tool results probabilistically. A vericoding pass would replace or augment this with a formal proof step for critical Rust tool runner logic, starting with `sandbox.rs` boundary invariants (already partially annotated with Verus proof stubs).

### 9.2 Tripartite cognitive memory architecture

Current agents — including this platform — operate primarily within a single context window. Even with the `stable_prefix` / `working_set` compression layer already implemented, all memory is ephemeral: session knowledge is discarded when the run ends.

A **Tripartite Cognitive Architecture** adds two persistent layers alongside the in-context working memory:

| Layer | Function | Implementation path |
|---|---|---|
| **Episodic** | Stores past events, failures, and outcomes across sessions; enables "I saw this bug pattern before" recall | Vectorized chronological event logs; semantic search over run history; already partially present via `loop_summaries` and `episodic_facts` in `memory.py` |
| **Semantic** | Encodes domain knowledge, API contracts, architectural rules, and codebase relationships | Knowledge graph backed by vector store; surfaced via MCP `resources/list`; already partially present via AST index and semantic hits in `context_builder.py` |
| **Procedural** | Stores validated action sequences for repeatable tasks (e.g. "run pytest in this repo") | Action cache / procedure cache; already partially present via `procedure_cache.py` |

The gap is persistence: episodic and semantic facts currently live only within a single run. Connecting `run_store.py` and `memory.py` to a vector-backed long-term store (SQLite FTS or pgvector) would complete this layer.

### 9.3 Cross-repository microservice orchestration

This platform currently operates on a single workspace directory. Enterprise software is built on distributed microservice architectures where a single feature may require synchronized changes across multiple repositories.

The path forward requires:

1. **Repository-level graph indexing** — a SCIP or REPOGRAPH-style index that maps symbol definitions and call sites across repo boundaries, allowing the planner to predict breaking changes before writing code.
2. **Sub-agent fan-out** — the `MetaOrchState` and `meta_graph.py` already define the data model for sub-agent decomposition. The missing piece is a scheduler that enforces dependency ordering (FrontendAgent waits for PaymentAgent to pass tests) and uses git worktree isolation to prevent parallel agents from conflicting on shared files.
3. **Multi-provider routing** — routing decisions that span sub-agent assignments, not just model selection within a single run.

### 9.4 Agentic self-healing testing loop

Beyond the current verify/retry loop, a self-healing testing agent would:

- **Continuously monitor** code commits and DOM/API changes without human initiation.
- **Risk-prioritize** test execution based on the blast radius of each change (using semantic memory of prior failures).
- **Auto-repair broken tests** when a legitimate application change causes a test to fail due to selector or API changes — committing the repaired test back to the repo.
- **Predict failures** before they manifest using historical defect patterns and synthetic edge-case generation.

The `verifier` node is the correct insertion point. The gap is: (a) test repair as a first-class plan step, not just a failure signal; (b) continuous monitoring mode (the platform currently runs in request/response mode only).

### 9.5 Kubernetes-native hardware sandboxing (IDEsaster mitigation)

The "IDEsaster" vulnerability class (documented in CVE-2025-49150, CVE-2025-53536) exploits the trust that AI coding environments place in their host OS. Indirect prompt injections — hidden in README files, third-party source code, or malicious MCP servers — can cause agents to modify IDE configuration files and achieve Remote Code Execution.

This platform already has significant mitigations:
- Command allowlist enforcement in `rs/runner/src/tools/exec.rs`
- Path boundary enforcement in `rs/runner/src/config.rs`
- Network-deny-by-default policy in config
- Prompt injection detection via `detect_prompt_injection()` in `rs/runner/src/sandbox.rs`
- MCP endpoint redaction metadata

The remaining gap is **host isolation**: the runner currently executes on the developer's machine or in a Docker container with shared kernel access. Full mitigation requires ephemeral gVisor or Kata Container sandboxes orchestrated via Kubernetes, where each tool invocation runs in a fresh, hardware-isolated environment that is destroyed after the task completes. Until then, the `sandbox.rs` `SafeFallback` mode (command allowlist + path scoping) is the practical defense perimeter.

**Kubernetes CRD sandbox** is listed in `## 4.2 Keep future-facing` and remains the correct classification for a project at this maturity stage. The `detect_prompt_injection()` function added in Wave 6 is the pragmatic near-term hardening step.

## Wave 9 — Persistent Cross-Session Memory and Neurosymbolic Verification

Status: **COMPLETE**

- [x] **Tripartite persistent memory:** vector-backed long-term store bridging [`py/src/lg_orch/run_store.py`](../py/src/lg_orch/run_store.py) and [`py/src/lg_orch/memory.py`](../py/src/lg_orch/memory.py) across sessions (semantic, episodic, and procedural tiers)
  - [`py/src/lg_orch/long_term_memory.py`](../py/src/lg_orch/long_term_memory.py): `LongTermMemoryStore` with three SQLite tables (`semantic_memories`, `episodic_memories`, `procedural_memories`); `semantic_fts` FTS5 virtual table; numpy float32 cosine similarity for semantic search; `stub_embedder()` for testing; `retrieve_for_context()` budget-capped cross-tier retrieval.
  - [`py/src/lg_orch/memory.py`](../py/src/lg_orch/memory.py): `build_context_layers()` accepts `long_term: LongTermMemoryStore | None`; injects retrieved memories as stable-prefix segment; persists loop summaries as episodes.
  - `OrchState.long_term_memory_path` field added.

- [x] **Neurosymbolic vericoding:** Verus/Dafny proof-checker loop for Rust runner boundary invariants
  - [`rs/runner/src/invariants.rs`](../rs/runner/src/invariants.rs): `Invariant` trait, `InvariantRequest`, `InvariantChecker` with four concrete invariants: `PathConfinementInvariant`, `CommandAllowlistInvariant`, `NoShellMetacharInvariant`, `ToolNameKnownInvariant`.
  - [`py/src/lg_orch/vericoding.py`](../py/src/lg_orch/vericoding.py): Python-side pre-check mirror `PythonInvariantChecker` with `InvariantViolation` typed exception.
  - Wired into [`rs/runner/src/tools/exec.rs`](../rs/runner/src/tools/exec.rs) and [`rs/runner/src/tools/fs.rs`](../rs/runner/src/tools/fs.rs) as a pre-validation layer.

- [x] **Cross-repository microservice orchestration:** SCIP/REPOGRAPH symbol index, multi-repo sub-agent fan-out via [`py/src/lg_orch/meta_graph.py`](../py/src/lg_orch/meta_graph.py)
  - [`py/src/lg_orch/scip_index.py`](../py/src/lg_orch/scip_index.py): `ScipIndex`, `ScipSymbol`, `load_scip_index()` reading `scip_index.json` sidecars; `cross_repo_deps()` for cross-index symbol matching.
  - [`py/src/lg_orch/multi_repo.py`](../py/src/lg_orch/multi_repo.py): `RepoConfig`, `CrossRepoHandoff`, `MultiRepoScheduler` wrapping `MetaGraphScheduler`; injects `repo_root`, `runner_url`, SCIP symbol summaries per task.

- [x] **Agentic self-healing testing loop:** continuous monitoring mode with test repair as a first-class plan step
  - [`py/src/lg_orch/healing_loop.py`](../py/src/lg_orch/healing_loop.py): `HealingLoop` with `poll_once()` (pytest subprocess), `run_until_cancelled()` continuous polling + `asyncio.TaskGroup` job dispatch; `HealingJob` state machine.
  - `POST /healing/start`, `POST /healing/{loop_id}/stop`, `GET /healing/{loop_id}/jobs` API endpoints.
  - `OrchState.test_repair_mode` and `healing_job_id` fields; REPAIR MODE planner prompt prefix.

- [x] **Kubernetes-native gVisor/Kata Container sandboxing:** replace command allowlist with hardware-enforced container boundaries
  - [`infra/k8s/runner-deployment.yaml`](../infra/k8s/runner-deployment.yaml): `runtimeClassName: gvisor`, hardened `securityContext` (`runAsNonRoot`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`), `seccompProfile: RuntimeDefault`, `emptyDir /workspace` volume.
  - [`infra/k8s/kata-runtime-class.yaml`](../infra/k8s/kata-runtime-class.yaml): Kata Containers `RuntimeClass` with `kata-qemu` handler.
  - [`infra/k8s/network-policy.yaml`](../infra/k8s/network-policy.yaml): `NetworkPolicy` blocking all external egress; allowing only DNS + orchestrator service.
  - [`rs/runner/src/config.rs`](../rs/runner/src/config.rs): `SandboxConfig` struct with `workspace_path` (default `/workspace`) and `enforce_read_only_root`; [`rs/runner/src/sandbox.rs`](../rs/runner/src/sandbox.rs): `validate_write_path()` enforcing workspace confinement.
  - [`py/src/lg_orch/k8s_sandbox.py`](../py/src/lg_orch/k8s_sandbox.py): `validate_deployment_manifest()` + `generate_sandbox_config_toml()`.

---

## Wave 10 — Production Hardening and Enterprise Features

**Goal:** Make the platform viable for production multi-tenant deployment: distributed tracing, horizontal scaling, RBAC, audit exports, and SLA-aware routing.

**Status:** PLANNED

### 10.1 Distributed tracing — OpenTelemetry span propagation Python↔Rust

Current state: [`py/src/lg_orch/trace.py`](../py/src/lg_orch/trace.py) emits structured JSON events; [`rs/runner/src/auth.rs`](../rs/runner/src/auth.rs) has no trace context extraction. Spans are not correlated across the HTTP boundary.

- **Python side** — add `opentelemetry-sdk` and `opentelemetry-otlp` to [`py/pyproject.toml`](../py/pyproject.toml); instrument [`py/src/lg_orch/tools/runner_client.py`](../py/src/lg_orch/tools/runner_client.py) to inject `traceparent` / `tracestate` W3C headers on every outbound HTTP request to the runner; instrument all node entry/exit points as child spans; configure OTLP exporter pointing at `OTEL_EXPORTER_OTLP_ENDPOINT` env var.
- **Rust side** — add `tracing-opentelemetry` and `opentelemetry-otlp` to [`rs/runner/Cargo.toml`](../rs/runner/Cargo.toml); extract `traceparent` from inbound request headers in [`rs/runner/src/auth.rs`](../rs/runner/src/auth.rs); attach extracted span context to the `tracing` subscriber so all child spans appear under the orchestrator trace.
- **Config** — expose `[telemetry] otlp_endpoint` in [`configs/runtime.prod.toml`](../configs/runtime.prod.toml); default to disabled in `runtime.dev.toml`.
- **Tests** — add integration test asserting that a single orchestration run produces a root span with child spans from both Python and Rust with matching `trace_id`.

### 10.2 Prometheus `/metrics` endpoint

- **Python orchestrator** — expose `GET /metrics` via `prometheus-client` on the same FastAPI app; instrument: active runs gauge, run duration histogram (by lane), LLM call counter (by provider/model), tool call counter (by tool name), approval pending gauge.
- **Rust runner** — expose `GET /metrics` via `metrics` + `metrics-exporter-prometheus` crates; instrument: tool execution duration histogram (by tool name), sandbox type counter, tool error counter (by error class), approval token verifications counter.
- **Kubernetes** — add `prometheus.io/scrape: "true"` annotations to [`infra/k8s/deployment.yaml`](../infra/k8s/deployment.yaml) and [`infra/k8s/runner-deployment.yaml`](../infra/k8s/runner-deployment.yaml).

### 10.3 Stateless orchestrator with distributed checkpoint store

Current state: [`py/src/lg_orch/checkpointing.py`](../py/src/lg_orch/checkpointing.py) uses `SqliteCheckpointSaver` which writes to a local file. This prevents horizontal scaling — all replicas must share the same SQLite file or checkpoints are lost on pod restart.

- **Redis backend** — implement `RedisCheckpointSaver(CheckpointSaver)` in [`py/src/lg_orch/checkpointing.py`](../py/src/lg_orch/checkpointing.py) using `redis.asyncio`; key schema: `checkpoint:{run_id}:{thread_ts}`; TTL: configurable via `[checkpointing] ttl_seconds` in runtime config.
- **Postgres backend** — implement `PostgresCheckpointSaver(CheckpointSaver)` as an alternative; table: `checkpoints(run_id TEXT, thread_ts TEXT, data JSONB, created_at TIMESTAMPTZ)`; use `asyncpg`.
- **Backend selector** — `checkpointing.py` `build_saver(config)` factory reads `[checkpointing] backend = "sqlite" | "redis" | "postgres"` from runtime config; `sqlite` remains default for dev.
- **`RunStore` namespace enforcement** — [`py/src/lg_orch/run_store.py`](../py/src/lg_orch/run_store.py) `namespace` column exists but is not enforced in queries; add `WHERE namespace = :namespace` to all `list_runs`, `get_run`, `update_run`, `delete_run` queries; `namespace` sourced from JWT `sub` claim (see section 10.5).

### 10.4 HorizontalPodAutoscaler for orchestrator pods

- Add [`infra/k8s/hpa.yaml`](../infra/k8s/hpa.yaml): `HorizontalPodAutoscaler` targeting the orchestrator `Deployment`; scale on CPU utilization (target 60%) and custom metric `orchestrator_active_runs` (target 5 per replica); min replicas: 2, max replicas: 10.
- Requires stateless checkpoint store (section 10.3) as a prerequisite.

### 10.5 RBAC and multi-tenant JWT isolation

Current state: [`py/src/lg_orch/remote_api.py`](../py/src/lg_orch/remote_api.py) has `auth_mode = "bearer"` in prod config but no JWT claims parsing. The `namespace` field in `RunStore` is populated but not enforced.

- **JWT middleware** — add FastAPI middleware in [`py/src/lg_orch/remote_api.py`](../py/src/lg_orch/remote_api.py) that: verifies RS256/HS256 JWT signature using `python-jose`; extracts `sub` claim as namespace; extracts `roles` claim for role-based checks; rejects requests with `401` if token is missing, expired, or has invalid signature.
- **Namespace injection** — pass extracted `sub` as `namespace` to all `RunStore` and `CheckpointSaver` operations so tenants cannot access each other's runs.
- **Role checks** — enforce that `POST /runs` requires `role: operator`; `DELETE /runs/{id}` requires `role: admin`; approval endpoints require `role: approver`.
- **Config** — expose `[auth] jwt_secret` (HS256) or `[auth] jwks_url` (RS256) in runtime config; `dev` profile may set `auth_mode = "none"` to bypass JWT for local development.

### 10.6 Audit log export

- **Structured JSONL** — [`py/src/lg_orch/trace.py`](../py/src/lg_orch/trace.py) already writes trace events; add `AuditLogger` that filters trace events matching approval decisions, run starts, run cancellations, and policy gate blocks, and writes them to a rolling JSONL file under `logs/audit/`.
- **S3/GCS sink** — add optional `[audit] sink = "s3" | "gcs"` config; use `aioboto3` / `google-cloud-storage` to upload completed JSONL segments.
- **Retention** — implement local rotation with configurable `[audit] retention_days`.

### 10.7 SLA-aware model routing with automatic degradation

- Extend [`py/src/lg_orch/model_routing.py`](../py/src/lg_orch/model_routing.py) with `SlaRoutingPolicy`: each lane carries a `latency_budget_ms` from runtime config; if the current provider's rolling p95 latency exceeds the budget, automatically demote to the next configured provider for that lane.
- Record p95 latency per provider using a sliding window of the last 50 calls; expose as `model_routing_p95_latency_ms` Prometheus gauge (by provider/lane).

### 10.8 GitOps deployment pipeline

- Add [`infra/k8s/argocd-app.yaml`](../infra/k8s/argocd-app.yaml): ArgoCD `Application` resource targeting `infra/k8s/` in the repository; sync policy: automated with self-heal; health checks: custom health check for `HealingLoop` CRD if present.
- Document Flux alternative in [`docs/deployment_digitalocean.md`](../docs/deployment_digitalocean.md).

---

## Wave 11 — Evaluation Correctness and Benchmarking

**Goal:** Move the eval suite from structural/routing correctness to outcome correctness. The current suite confirms that the graph routes properly; it does not confirm that the agent produces working code. This wave makes eval the primary quality gate.

**Status:** PLANNED

### Current eval gap

All existing eval fixtures (`test-repair`, `real-world-repair`) set `_runner_enabled: False` in their task definitions. This means no actual code is executed, no diff is applied, and no pytest run is performed. The golden file assertions test graph shape and routing metadata — not correctness.

### 11.1 Enable runner execution for existing fixtures

- Set `_runner_enabled: True` in [`eval/tasks/test-repair.json`](../eval/tasks/test-repair.json) and [`eval/tasks/real_world_repair.json`](../eval/tasks/real_world_repair.json).
- Update [`eval/run.py`](../eval/run.py) to: (1) apply the produced diff to the fixture `src/` directory; (2) run `pytest` on the fixture `tests/` directory inside the runner sandbox; (3) assert that pytest exits 0 after patch application. Use the `benchmark_class`, `target_file`, and `target_function` fields already present in `EvalTask` to scope the pytest invocation.
- Add a `post_apply_pytest_pass: bool` field to the golden file assertion schema; update [`eval/golden/test-repair.json`](../eval/golden/test-repair.json) and [`eval/golden/real-world-repair.json`](../eval/golden/real-world-repair.json).

### 11.2 pass@k scoring

- Add `--pass-at-k K` argument to [`eval/run.py`](../eval/run.py); when `K > 1`, run each task `K` times independently and compute `pass@k = 1 - C(n-c, k) / C(n, k)` where `n=K` and `c` is the number of passing runs.
- Emit `pass_at_k` field in `eval/run.py` JSON output alongside existing `pass_rate`.
- Default `K=1` to preserve backward compatibility with CI pipeline.

### 11.3 SWE-bench lite adapter

- Add [`eval/tasks/swe_bench_lite/`](../eval/tasks/swe_bench_lite/) directory containing 10–20 curated SWE-bench lite task definitions converted to the platform's `EvalTask` schema.
- Fields required per task: `instance_id`, `repo`, `base_commit`, `problem_statement`, `patch` (ground truth for reference), `test_cmd`, `fail_to_pass` test list, `pass_to_pass` test list.
- Add [`eval/run.py`](../eval/run.py) `--dataset swe-bench-lite` flag that: (1) clones/fetches the target repo at `base_commit` into an isolated worktree; (2) submits the `problem_statement` as the run objective; (3) collects the produced diff; (4) applies the diff; (5) runs `test_cmd`; (6) asserts all `fail_to_pass` tests now pass and all `pass_to_pass` tests still pass.
- Report resolved rate (SWE-bench standard metric) in addition to `pass@k`.

### 11.4 Correctness eval CI job

- Add `eval-correctness` job to [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) that runs `python eval/run.py --dataset test-repair --pass-at-k 3`; fails if `pass@3 < 0.67`.
- Run nightly (not on every commit) to avoid LLM API cost at CI time; schedule: `cron: "0 2 * * *"`.

---

## Wave 12 — Streaming Completeness and UX Parity

**Goal:** Token-level streaming must flow end-to-end from every LLM call site through the SSE endpoint to both the SPA and the VS Code extension. The current implementation streams only `interactive` lane nodes; `coder`, `reporter`, and all non-interactive nodes block until the full completion arrives.

**Status:** PLANNED

### 12.1 Current streaming coverage gap

- [`py/src/lg_orch/nodes/router.py`](../py/src/lg_orch/nodes/router.py) and [`py/src/lg_orch/nodes/planner.py`](../py/src/lg_orch/nodes/planner.py): stream path wired for `interactive` lane only.
- [`py/src/lg_orch/nodes/coder.py`](../py/src/lg_orch/nodes/coder.py): no streaming path; blocks on full completion.
- [`py/src/lg_orch/nodes/reporter.py`](../py/src/lg_orch/nodes/reporter.py): no streaming path; blocks on full completion.
- `GET /runs/{id}/stream` SSE endpoint in [`py/src/lg_orch/remote_api.py`](../py/src/lg_orch/remote_api.py): carries `node_start`, `node_end`, `tool_result` lifecycle events only; no `llm_chunk` events.
- [`py/src/lg_orch/spa/main.js`](../py/src/lg_orch/spa/main.js) and the VS Code extension: consume only lifecycle events; token chunks are not rendered incrementally.

### 12.2 `llm_chunk` event emission from all nodes

- Add `push_run_event(run_id, {"type": "llm_chunk", "node": node_name, "text": chunk})` calls inside the async generator loops in every node that calls `InferenceClient.chat_completion_stream()`. The `push_run_event` helper already exists in [`py/src/lg_orch/remote_api.py`](../py/src/lg_orch/remote_api.py).
- Wire streaming path in [`py/src/lg_orch/nodes/coder.py`](../py/src/lg_orch/nodes/coder.py): replace the blocking `chat_completion()` call with `chat_completion_stream()` and accumulate chunks; emit one `llm_chunk` event per token batch (or per chunk as received).
- Wire streaming path in [`py/src/lg_orch/nodes/reporter.py`](../py/src/lg_orch/nodes/reporter.py): same pattern as coder.
- Extend the `interactive` lane path in [`py/src/lg_orch/nodes/router.py`](../py/src/lg_orch/nodes/router.py) to emit `llm_chunk` events (currently only the stream result is accumulated, not forwarded to SSE).

### 12.3 Router LLM call — demote keyword classifier to fast-path only

Current state: [`py/src/lg_orch/nodes/router.py`](../py/src/lg_orch/nodes/router.py:30) uses `_classify_intent()` keyword matching as the primary classifier. An LLM call is only made when `provider != "local"`. The "router as topology selector" vision in [`docs/agent_collaboration_2026.md`](agent_collaboration_2026.md) is half-implemented.

- Invert the priority: always use LLM inference for routing decisions; use `_classify_intent()` only as a fast-path cache hit for trivially classifiable inputs (e.g. single-word commands).
- The LLM call must use the `interactive` lane (lowest latency model tier) with a token budget of ≤ 200 tokens.
- Emit the routing decision as a `llm_chunk`-compatible stream so the SPA can show the router "thinking."
- Add `router_used_llm: bool` and `router_confidence: float` fields to `OrchState` for telemetry.

### 12.4 SPA token chunk rendering

- Update [`py/src/lg_orch/spa/main.js`](../py/src/lg_orch/spa/main.js) to handle `{"type": "llm_chunk", "node": ..., "text": ...}` SSE events: append `text` to the activity stream entry for the named node; render progressively without waiting for `node_end`.
- Add a typing-cursor animation (`|` blinking) to the active node panel while chunks are arriving.
- Cap the visible rolling buffer to the last 2000 characters per node to prevent DOM growth.

### 12.5 VS Code extension token chunk rendering

- Update [`vscode-extension/src/RunPanelProvider.ts`](../vscode-extension/src/RunPanelProvider.ts) to forward `llm_chunk` events from the SSE stream to the webview panel; render token chunks in the active node's output area incrementally.

---

## Wave 13 — Production Hardening: Sandbox, Tooling, and Schema Correctness

**Goal:** Close the remaining correctness and security gaps in the runner sandbox, SCIP toolchain integration, inference client, schema enforcement, and multi-tenant isolation. These are pre-production correctness items that do not require new features, only correct implementations of already-designed components.

**Status:** PLANNED

### 13.1 cgroup v2 resource limits for LinuxNamespace sandbox

Current state: [`rs/runner/src/sandbox.rs`](../rs/runner/src/sandbox.rs) `LinuxNamespace` path calls `unshare(CLONE_NEWPID | CLONE_NEWNET | CLONE_NEWNS)` but applies no cgroup v2 resource limits. A sandboxed process can exhaust CPU, memory, or pids on the host.

- Before calling `unshare`, write the process to a new cgroup v2 hierarchy: `echo $PID > /sys/fs/cgroup/lg-runner/{run_id}/cgroup.procs`.
- Apply limits from `SandboxConfig`: `cpu.max` (e.g. `100000 100000` = 100% of one core), `memory.max` (e.g. `512M`), `pids.max` (e.g. `64`).
- Read limit values from [`rs/runner/src/config.rs`](../rs/runner/src/config.rs) `SandboxConfig` fields `cpu_quota_us`, `memory_limit_bytes`, `pids_max`; expose in [`configs/runtime.prod.toml`](../configs/runtime.prod.toml).
- On cleanup, remove the cgroup: `rmdir /sys/fs/cgroup/lg-runner/{run_id}`.
- Gate behind `cfg!(target_os = "linux")` compile flag; no-op on macOS/Windows dev builds.
- Add integration test asserting that a process exceeding `memory.max` is killed with `SIGKILL`.

### 13.2 Firecracker REST API correctness

Current state: [`rs/runner/src/tools/exec.rs`](../rs/runner/src/tools/exec.rs:135) `MicroVmEphemeral` path passes `--kernel` and `--rootfs` as CLI args to a `firecracker` binary. This is a stub architecture — real Firecracker does not accept those arguments; it exposes a Unix socket REST API.

**Decision required (choose one):**

**Option A — Implement real Firecracker REST client:**
- Replace the CLI invocation in [`rs/runner/src/tools/exec.rs`](../rs/runner/src/tools/exec.rs) with a `FirecrackerClient` struct that: (1) spawns `firecracker --api-sock /tmp/firecracker-{id}.sock`; (2) issues `PUT /machine-config` with `vcpu_count`, `mem_size_mib`; (3) issues `PUT /boot-source` with `kernel_image_path`, `boot_args`; (4) issues `PUT /drives/rootfs` with `path_on_host`, `is_root_device: true`, `is_read_only: false`; (5) issues `PUT /actions {"action_type": "InstanceStart"}`; (6) polls `GET /` until VM is ready; (7) runs the tool command via the guest agent; (8) issues `PUT /actions {"action_type": "SendCtrlAltDel"}` to shut down.
- Add `firecracker_socket_path` to `SandboxConfig`.

**Option B — Demote MicroVmEphemeral to tech debt and use LinuxNamespace as production tier:**
- Remove or `#[cfg(feature = "firecracker")]` gate the `MicroVmEphemeral` arm in [`rs/runner/src/tools/exec.rs`](../rs/runner/src/tools/exec.rs).
- Document in [`docs/architecture.md`](../docs/architecture.md) that `LinuxNamespace` + cgroup v2 + gVisor Kubernetes runtime is the supported production sandbox tier.
- Add `WARN` log if `sandbox_mode = "MicroVmEphemeral"` is configured in non-dev builds.

Recommended: implement Option B immediately and track Option A as a future milestone.

### 13.3 SCIP toolchain integration — `scripts/index_repo.sh`

Current state: [`py/src/lg_orch/scip_index.py`](../py/src/lg_orch/scip_index.py) reads `scip_index.json` sidecar files that must be pre-generated externally. There is no toolchain that produces them. Without this, all cross-repo dependency analysis in [`py/src/lg_orch/multi_repo.py`](../py/src/lg_orch/multi_repo.py) operates on empty indexes.

- Add [`scripts/index_repo.sh`](../scripts/index_repo.sh): shell script that: (1) detects repo language (Python: run `scip-python index --project-root . --output scip_index.json`; Rust: run `rust-analyzer scip .` to emit SCIP); (2) converts SCIP binary format to the `scip_index.json` sidecar schema expected by [`py/src/lg_orch/scip_index.py`](../py/src/lg_orch/scip_index.py) using `scip convert --from scip.bin --to json`; (3) writes the sidecar to `{repo_root}/scip_index.json`.
- Add `scripts/index_repo.sh` invocation to the `bootstrap_local.cmd` / `bootstrap_local.sh` scripts.
- Document the sidecar schema and toolchain requirements in [`docs/architecture.md`](../docs/architecture.md).
- Add a CI step that generates and validates the sidecar for the platform's own Python codebase.

### 13.4 InferenceClient function calling / tool calling support

Current state: [`py/src/lg_orch/tools/inference_client.py`](../py/src/lg_orch/tools/inference_client.py) sends freeform chat completions only. It does not support the `tools` parameter for OpenAI-format structured tool calls.

- Add `tools: list[dict] | None = None` and `tool_choice: str | dict | None = None` parameters to `InferenceClient.chat_completion()` and `chat_completion_stream()`.
- When `tools` is provided, include `"tools": tools` and `"tool_choice": tool_choice` in the request body.
- Parse `tool_calls` from the response `message` and return them as a typed `ToolCallResult` alongside the text content.
- Add `FunctionCallingClient` thin wrapper in [`py/src/lg_orch/tools/inference_client.py`](../py/src/lg_orch/tools/inference_client.py) that accepts a `Callable` registry and auto-dispatches tool calls.
- Update [`py/tests/test_inference_client.py`](../py/tests/test_inference_client.py) with mock-based tests for function call request construction and response parsing.

### 13.5 Verifier report JSON schema enforcement

Current state: [`schemas/verifier_report.schema.json`](../schemas/verifier_report.schema.json) exists but [`py/src/lg_orch/nodes/verifier.py`](../py/src/lg_orch/nodes/verifier.py) builds its output from Python dataclasses without validating the emitted JSON against the schema at runtime.

- Add `jsonschema.validate(report_dict, VERIFIER_REPORT_SCHEMA)` call in [`py/src/lg_orch/nodes/verifier.py`](../py/src/lg_orch/nodes/verifier.py) before returning the verifier report; raise `VerifierSchemaError` (subclass of `ValueError`) on validation failure.
- Load `schemas/verifier_report.schema.json` at module import time using `importlib.resources`; cache as a module-level constant.
- Add schema validation to the CI pipeline's `lint-python` job: `python -c "import jsonschema; jsonschema.validate(json.load(open('eval/golden/test-repair.json')), ...)"`.
- Apply the same pattern to `schemas/planner_output.schema.json` in [`py/src/lg_orch/nodes/planner.py`](../py/src/lg_orch/nodes/planner.py) and `schemas/tool_envelope.schema.json` in [`rs/runner/src/envelope.rs`](../rs/runner/src/envelope.rs) (Rust: `serde` + `jsonschema` crate).

---

## 10. Market Parity Status

This section tracks the platform's competitive position against the three primary reference points: LangGraph Cloud, AutoGen, and CrewAI.

| Feature | LangGraph Cloud | AutoGen | CrewAI | This Platform | Gap Status |
|---|---|---|---|---|---|
| Stateful graph orchestration | ✅ Native | Partial | Partial | ✅ Complete (Wave 3) | CLOSED |
| Streaming token output | ✅ Full end-to-end | ✅ Full | Partial | ⚠️ Interactive lane only | OPEN — Wave 12 |
| Approval / human-in-the-loop | ✅ Interrupt API | Partial | No | ✅ Complete (Wave 8) | CLOSED |
| Multi-agent fan-out | ✅ Subgraph | ✅ GroupChat | ✅ Crew | ✅ Complete (Wave 8) | CLOSED |
| Persistent checkpoint store | ✅ Redis/Postgres | No | No | ⚠️ SQLite only | OPEN — Wave 10.3 |
| Horizontal scaling | ✅ Managed | No | No | ⚠️ Single replica | OPEN — Wave 10.3/10.4 |
| RBAC / multi-tenant | ✅ Managed | No | No | ⚠️ Bearer token, no JWT claims | OPEN — Wave 10.5 |
| Distributed tracing (OTEL) | ✅ Native | No | No | ⚠️ Custom JSON events only | OPEN — Wave 10.1 |
| Prometheus metrics | ✅ Native | No | No | ⚠️ Not present | OPEN — Wave 10.2 |
| Formal correctness eval (pass@k) | No | No | No | ⚠️ Structural only | OPEN — Wave 11 |
| SWE-bench adapter | No | No | No | ⚠️ Not present | OPEN — Wave 11 |
| Token-level streaming SPA | Partial | No | No | ⚠️ Lifecycle events only | OPEN — Wave 12 |
| Function calling in inference client | ✅ Native | ✅ Native | ✅ Native | ⚠️ Freeform only | OPEN — Wave 13.4 |
| Sandboxed execution | Limited | No | No | ✅ gVisor/LinuxNamespace (Wave 9) | CLOSED |
| cgroup v2 resource limits | N/A | No | No | ⚠️ No resource limits | OPEN — Wave 13.1 |
| SCIP cross-repo indexing | No | No | No | ⚠️ Sidecar reads only, no toolchain | OPEN — Wave 13.3 |
| Signed approval tokens | N/A | N/A | N/A | ⚠️ Plain string, no HMAC | OPEN — Wave 0 |
| Self-healing test loop | No | No | No | ✅ Complete (Wave 9) | CLOSED |
| Neurosymbolic verification | No | No | No | ✅ Invariant checker (Wave 9) | CLOSED |
| Long-term memory (tripartite) | No | Partial | No | ✅ Complete (Wave 9) | CLOSED |
| Git worktree branch isolation | No | No | No | ✅ Complete (Wave 8) | CLOSED |
| VS Code extension | No | No | No | ✅ Complete (Wave 7) | CLOSED |

### Priority order for remaining open gaps

1. **Wave 0** — Approval token HMAC: security prerequisite, no deployment without it.
2. **Wave 10.5** — JWT/RBAC: blocks multi-tenant deployment.
3. **Wave 10.3** — Distributed checkpoint store: blocks horizontal scaling.
4. **Wave 13.4** — InferenceClient function calling: required for structured tool orchestration.
5. **Wave 12** — Full streaming: highest perceived-latency gap vs. competitors.
6. **Wave 11** — Correctness eval: required before claiming benchmark parity.
7. **Wave 13.1** — cgroup v2: sandbox hardening.
8. **Wave 13.3** — SCIP toolchain: unlocks cross-repo analysis.
9. **Wave 13.5** — Schema enforcement: correctness gate.
10. **Wave 10.1/10.2** — OTEL + Prometheus: observability for production ops.
