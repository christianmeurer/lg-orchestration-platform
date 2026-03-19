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
- There is no explicit “stable prefix” layer containing durable repo facts versus an “ephemeral working set” layer containing loop-local evidence.

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

The right target is not “full SOTA everywhere.” It is a narrow set of changes that make the current architecture materially more autonomous and observable without turning the repository into an experimental multi-agent platform.

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
   - After each failed loop, store a compressed “what changed / what failed / what to try next” fact pack for the next planner pass.

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

- [ ] **Tripartite persistent memory:** vector-backed long-term store bridging [`py/src/lg_orch/run_store.py`](../py/src/lg_orch/run_store.py) and [`py/src/lg_orch/memory.py`](../py/src/lg_orch/memory.py) across sessions (semantic, episodic, and procedural tiers)
- [ ] **Neurosymbolic vericoding:** Verus/Dafny proof-checker loop for Rust runner boundary invariants
- [ ] **Cross-repository microservice orchestration:** SCIP/REPOGRAPH symbol index, multi-repo sub-agent fan-out via [`py/src/lg_orch/meta_graph.py`](../py/src/lg_orch/meta_graph.py)
- [ ] **Agentic self-healing testing loop:** continuous monitoring mode with test repair as a first-class plan step
- [ ] **Kubernetes-native gVisor/Kata Container sandboxing:** replace command allowlist with hardware-enforced container boundaries
