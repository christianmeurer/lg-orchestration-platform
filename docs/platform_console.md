# Platform Console (Visualization Scheme)

This document defines a simple, extensible visualization scheme for the LG Orchestration Platform.

## Goals

- Provide a stable mental model: *graph* + *timeline* + *artifacts*.
- Make every run inspectable via a single JSON trace artifact.
- Keep the first implementation file-based (no DB required).

## 1) Graph view

The orchestrator can export a Mermaid graph for the current topology.

- Export source: [`export_mermaid()`](../py/src/lg_orch/graph.py:38)
- CLI: `uv run python -m lg_orch.main export-graph`

Render by pasting the output into any Mermaid renderer.

## 2) Timeline view

Each run emits a trace file (JSON) containing an ordered event list.

- Trace implementation: [`write_run_trace()`](../py/src/lg_orch/trace.py:29)
- Node events emitted in: [`ingest()`](../py/src/lg_orch/nodes/ingest.py:8), [`policy_gate()`](../py/src/lg_orch/nodes/policy_gate.py:8), [`context_builder()`](../py/src/lg_orch/nodes/context_builder.py:9), [`planner()`](../py/src/lg_orch/nodes/planner.py:21), [`executor()`](../py/src/lg_orch/nodes/executor.py:8), [`verifier()`](../py/src/lg_orch/nodes/verifier.py:8), [`reporter()`](../py/src/lg_orch/nodes/reporter.py:8)

Event kinds:

- `node`: transitions and node-level metadata
- `tools`: batched tool calls executed by the runner

Example trace location:

- `artifacts/runs/run-<run_id>.json`

## 3) Artifacts view

Artifacts are referenced from the trace and/or exist on disk:

- patches/diffs (future)
- verification reports (future)
- runner stdout/stderr envelopes (present in `tool_results`)

## 4) Config switches

Tracing is enabled by runtime profile.

- Dev: [`configs/runtime.dev.toml`](../configs/runtime.dev.toml:1)
- Stage: [`configs/runtime.stage.toml`](../configs/runtime.stage.toml:1)
- Prod: [`configs/runtime.prod.toml`](../configs/runtime.prod.toml:1)

Keys:

```toml
[trace]
enabled = true
output_dir = "artifacts/runs"
```

## 5) Next iteration (optional)

- Add an HTTP API exposing:
  - `/v1/runs` list
  - `/v1/runs/{run_id}` trace
- Add a small web UI that renders:
  - Mermaid graph
  - timeline from trace events
  - artifacts explorer

