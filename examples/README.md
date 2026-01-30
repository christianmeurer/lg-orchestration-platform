# Examples

## 1) Local run (no runner required)

```bash
cd py
uv sync
uv run lg-orch run "summarize repo" --trace
```

Trace output is controlled by [`configs/runtime.dev.toml`](../configs/runtime.dev.toml:1) and written by [`write_run_trace()`](../py/src/lg_orch/trace.py:29).

## 2) Local run with runner

Terminal A:

```bash
cd rs/runner
cargo run
```

Terminal B:

```bash
cd py
uv sync
uv run lg-orch run "summarize repo" --runner-base-url http://127.0.0.1:8088 --trace
```

## 3) Export Mermaid graph

```bash
cd py
uv run lg-orch export-graph
```

