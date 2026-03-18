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

1. **Live run console with streaming timeline.** The current SPA (served at `GET /`) renders a static trace after run completion. Replace with a WebSocket or SSE-backed live view that shows each graph node activating, tool calls appearing as they execute, and the verifier result in real-time. Use an animated node-based graph diagram (e.g. Mermaid.js or a D3 force graph) that highlights the active node during execution.

2. **Agent activity visualization.** Render the actual graph topology inline alongside the run: nodes pulse as they activate, edges animate in the direction of data flow, the current lane (`interactive`, `deep_planning`, `recovery`) is highlighted. Inspired by Replit's agent trace view and Cursor's diff-flow visualization.

3. **Verifier report panel with inline diffs.** When `apply_patch` runs, show a GitHub-style diff inline (unified diff with syntax highlighting, language-aware coloring). When verification fails, highlight the specific check and the recovery path chosen. Approval buttons for gated exec calls appear inline in the activity stream — no need to navigate away.

4. **Run history and search.** A persistent left-panel run history with request text, duration, verification status, and model used. Full-text search over past runs. Clicking a run loads the full trace view with all the above components.

5. **Responsive, design-system-quality typography and layout.** Apply principles from modern developer tool design (VS Code dark theme system, Vercel's dashboard aesthetics, Linear's motion design): clean monospace code blocks, subtle animated transitions between states, semantic color coding for success/failure/warning, and a layout that works at 1024px and 1440px. Use a design system (Tailwind CSS + shadcn/ui components or a hand-rolled equivalent served from the Python API's static asset layer) without adding a Node.js build dependency to the runtime image.

6. **VSCode extension premium UX.** Beyond functional correctness, the extension panel should feel native to VS Code: use the VS Code Webview API with the `vscode-webview-ui-toolkit` component library, respect the editor's active color theme, and animate agent activity directly in the sidebar without requiring a browser. Inline diffs appear in the actual editor gutter, not in a separate panel.

Design references:
- [Vercel AI Playground](https://sdk.vercel.ai) — streaming token visualization
- [Replit Ghostwriter Agent](https://replit.com/ai) — live agent trace with node graph
- [Cursor composer](https://cursor.sh) — multi-file diff approval flow
- [Linear](https://linear.app) — motion design and transition polish
- VS Code [Webview API](https://code.visualstudio.com/api/extension-guides/webview) — native editor UX patterns

Status: **In Progress.** The live SPA and VS Code extension now ship approval actions, approval history, checkpoint visibility, inline diffs, run history, and verifier output. The remaining work is premium polish and richer interaction design rather than missing operator fundamentals.

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

Goal:

#### A. Contract-first collaborative agents

1. **Add an explicit coder specialist.**
   - Insert a coder node between planner and executor so patch synthesis is no longer implicit inside the planner.
2. **Introduce a uniform handoff envelope.**
   - Planner, coder, verifier, and recovery paths should exchange structured artifacts containing objective, file scope, evidence, constraints, acceptance checks, retry budget, and provenance.
3. **Retune the router into a topology selector.**
   - The router should decide not only lane and model tier, but also whether the next path is planner-only, planner-to-coder, or verifier-driven repair.
4. **Make verifier-directed retries specialist-aware.**
   - Localized implementation failures should route to coder; broader contract or architecture failures should route back to planner or context rebuilding.
5. **Keep execution deterministic.**
   - Do not collapse execution back into free-form LLM behavior. The Rust runner boundary remains a core design strength.

#### B. Governed-autonomy control plane

1. **Approval API.**
   - Add approve/reject endpoints so pending operations can be acted on through the API rather than only displayed in UI.
2. **Suspend/resume orchestration.**
   - Treat approval-required execution as a first-class suspended run state and resume from checkpoints after approval.
3. **Durable approval state and audit trail.**
   - Persist approver identity, challenge id, timestamps, operation class, and affected paths in the run store and trace artifacts.
4. **Client wiring.**
   - Replace placeholder approval buttons in the SPA and VS Code extension with real API-backed actions.
5. **Eval coverage.**
   - Add approval-path tasks covering block-before-approval, resume-after-approval, reject termination, and audit metadata preservation.

Roadmap before coding:

1. **Slice 1 — contracts first.**
   - Extend state and schemas with handoff envelopes and coder-facing task contracts.
2. **Slice 2 — explicit coder loop.**
   - Add the coder node, wire planner-to-coder-to-executor flow, and update verifier retry routing.
3. **Slice 3 — governed execution.**
   - Add approval endpoints, suspended run state, durable approval persistence, and resume logic.
4. **Slice 4 — client surfaces and evals.**
   - Wire SPA and VS Code controls to the approval API and cover the new control-flow paths in eval tasks and tests.

Expected outcome:

- Lula becomes a clearer multi-role system inside a single run: router, planner, coder, deterministic executor, verifier, reporter.
- Risky actions become observable, pausable, resumable, and auditable across API, SPA, and VS Code.
- The platform moves closer to enterprise-grade parity without prematurely expanding into uncontrolled multi-agent behavior.

Status: **In Progress.** The repository now has explicit planner → coder → executor flow, typed handoff envelopes, coder-targeted verifier retries, suspended run state, approve/reject endpoints, durable approval audit persistence, SPA/VS Code approval controls, and approval/suspend-resume eval coverage. Remaining work is refinement, richer operator workflows, and deeper scheduler evolution.

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
