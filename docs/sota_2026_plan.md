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

Goal:

- Align docs with the actual graph, router, trace, and checkpoint behavior.
- Freeze a realistic baseline before adding new product surface.

### Wave 2: first usable product surface

Targets:

- [`py/src/lg_orch/main.py`](../py/src/lg_orch/main.py)
- [`py/src/lg_orch/trace.py`](../py/src/lg_orch/trace.py)
- [`py/src/lg_orch/visualize.py`](../py/src/lg_orch/visualize.py)
- new minimal UI/API files

Goal:

- Turn existing graph export and trace artifacts into a first usable run viewer.
- Start with graph, timeline, final output, and tool results rather than a full control plane.

### Wave 3: run API and persistence

Targets:

- [`py/src/lg_orch/main.py`](../py/src/lg_orch/main.py)
- [`py/src/lg_orch/checkpointing.py`](../py/src/lg_orch/checkpointing.py)
- [`py/src/lg_orch/trace.py`](../py/src/lg_orch/trace.py)
- storage/API files to be introduced

Goal:

- Add run listing, run detail, replay metadata, and durable storage.
- Prefer SQLite first; leave Postgres and multi-user concerns for later.

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

The repository is already past the “toy scaffold” stage. The next correct move is not to rebuild core orchestration; it is to package what already works into a usable product surface, add durable run state, make provider choice more portable, and then tighten recovery/evaluation quality until the system is visibly competitive.
