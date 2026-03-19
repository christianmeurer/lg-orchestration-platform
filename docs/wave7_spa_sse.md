# Wave 7 / Gap 1 — Live SSE Streaming SPA

**Status:** In Progress  
**Author:** Roo (code mode)  
**Created:** 2026-03-19

---

## Objective

Add a live-streaming operator console backed by Server-Sent Events, served as a
standalone SPA from `py/src/lg_orch/spa/`. Complements the existing static trace
site (`visualize.py`) which remains untouched.

---

## Key Architectural Decisions

### 1. No FastAPI / Starlette

`remote_api.py` uses Python's stdlib `http.server.ThreadingHTTPServer`.  
The task description references FastAPI syntax, but the real server is stdlib.

**Decision:** All new code stays within the existing stdlib HTTP server.  
`create_spa_router()` returns a simple callable `(subpath: str) -> tuple[int, str, bytes]`
instead of a Starlette app. The `app.mount(...)` call from the spec is replaced by
routing entries in `_api_http_response()`.

### 2. queue.Queue, not asyncio.Queue

The HTTP handler threads are OS threads (ThreadingHTTPServer). `asyncio.Queue` is
not thread-safe and requires an event loop. All SSE queues use `queue.Queue` instead.

**Module-level state added to `remote_api.py`:**

```python
import queue as _queue
_run_streams: dict[str, _queue.Queue[dict[str, Any] | None]] = {}
_run_streams_lock = threading.Lock()
```

`push_run_event(run_id, event)` is the public API for other modules to push events
into an active SSE stream.

### 3. Two SSE routes

| Route | Purpose |
|---|---|
| `GET /v1/runs/{id}/stream` | **Existing** — polls `stream_run_sse()`, sends full run payload every 600 ms |
| `GET /runs/{id}/stream`    | **New** — drains `_run_streams[run_id]` Queue, sends individual trace events |

The new route is what the new SPA uses. The old `/v1/` route stays for backward compat.

### 4. SSE Protocol (new endpoint)

```
data: {"type":"node_start","node":"coder","ts_ms":1234567890}\n\n
data: {"type":"llm_chunk","text":"..."}\n\n
data: {"type":"done"}\n\n        ← stream terminator
```

Behaviour:
- **Active run**: SSE handler registers a Queue, drains it; polls run status every 600 ms
  as fallback; sends `data: {"type":"done"}\n\n` when run finishes.
- **Completed run**: reads the last known run payload from `service.get_run()`, sends it
  once as a `data:` frame, then immediately sends the done sentinel.
- **Unknown run**: sends HTTP 404 *before* opening the stream.
- Client disconnect: `OSError` on `wfile.write()` ⟹ break loop, remove queue.

### 5. SPA Files

```
py/src/lg_orch/spa/
├── __init__.py      Module docstring only
├── router.py        create_spa_router(spa_dir) → callable dispatcher
├── index.html       Self-contained (inline <style> + <script>); works opened from disk
├── style.css        Unminified human-readable copy of the same styles
└── main.js          Commented human-readable copy of the same JS
```

`router.py` maps URL subpaths:
- `/` or unknown → serve `index.html` (SPA catch-all)
- `/style.css`   → serve `style.css`
- `/main.js`     → serve `main.js`

The SPA is available at `/app` (or `/app/` or `/app/anything`).

### 6. SPA API calls

The SPA (index.html) calls:

| Fetch | Purpose |
|---|---|
| `GET /v1/runs` | Run history list |
| `GET /runs/{id}/stream` | Live SSE stream (new endpoint) |
| `POST /v1/runs/{id}/approve` | Approve pending operation |
| `POST /v1/runs/{id}/reject` | Reject pending operation |

### 7. Scope — what is NOT changed

- `visualize.py` — zero modifications; all CLI commands remain intact
- `trace.py` — zero modifications
- `run_store.py` — zero modifications
- `main.py` — zero modifications

---

## Implementation Sequence

1. **`remote_api.py`** — add:
   - `_run_streams`, `_run_streams_lock`, `push_run_event()`
   - routing entry for `GET /runs/{run_id}/stream` in `_api_http_response()`
   - `_stream_new_sse()` helper that drains the queue
   - HTTP handler branch in `RemoteAPIRequestHandler._handle_request()`
   - routing entries for `/app/*` to dispatch to the SPA router

2. **`spa/__init__.py`** — docstring

3. **`spa/style.css`** — GitHub dark palette; flexbox layout

4. **`spa/main.js`** — vanilla JS; EventSource; run list; approval buttons; node graph

5. **`spa/index.html`** — fully self-contained; imports style + script inline

6. **`spa/router.py`** — `create_spa_router(spa_dir)` callable

7. **Tests** — extend `test_remote_api.py` + create `test_spa.py`

---

## Node Pipeline (for the graph view in the SPA)

Nodes in execution order (from `graph.py` / `nodes/`):

```
ingest → policy_gate → context_builder → router → planner
      → coder → executor → verifier → reporter
```

The SPA renders these in a horizontal row and highlights the active node on
`node_start` events.

---

## Color Scheme (GitHub dark `#0d1117`)

| Token | Value |
|---|---|
| Background | `#0d1117` |
| Surface | `#161b22` |
| Border | `#30363d` |
| Text | `#c9d1d9` |
| Accent / link | `#58a6ff` |
| OK / green | `#3fb950` |
| Error / red | `#f85149` |
| Warning | `#d29922` |

Event row colors:
- `node_start` — `#555` (muted)
- `llm_chunk` — `#c9d1d9` (text)
- `tool_call` — `#58a6ff` (accent)
- `tool_result` — `#39d353` (green)
- `verifier_pass` — `#3fb950`
- `verifier_fail` / `error` — `#f85149` bold

---

## Progress Tracker

| Step | File | Status |
|---|---|---|
| Read source files | multiple | ✅ Done |
| Design doc | `docs/wave7_spa_sse.md` | ✅ Done |
| SSE queue infra | `remote_api.py` | ⬜ Pending |
| `/runs/{id}/stream` route | `remote_api.py` | ⬜ Pending |
| SPA `__init__.py` | `spa/__init__.py` | ⬜ Pending |
| SPA `style.css` | `spa/style.css` | ⬜ Pending |
| SPA `main.js` | `spa/main.js` | ⬜ Pending |
| SPA `index.html` | `spa/index.html` | ⬜ Pending |
| SPA `router.py` | `spa/router.py` | ⬜ Pending |
| Wire SPA in handler | `remote_api.py` | ⬜ Pending |
| SSE tests | `test_remote_api.py` | ⬜ Pending |
| SPA tests | `test_spa.py` | ⬜ Pending |
| ruff clean | — | ⬜ Pending |
| mypy clean | — | ⬜ Pending |
