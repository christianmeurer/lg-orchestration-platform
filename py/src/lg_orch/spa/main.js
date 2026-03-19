/**
 * main.js — LG Orchestration Platform Live Console
 *
 * Vanilla JS, no build step, no npm.
 * Talks to the ThreadingHTTPServer in remote_api.py via:
 *   GET  /v1/runs            — run history list
 *   GET  /runs/{id}/stream   — SSE live event stream (Wave 7 endpoint)
 *   POST /runs/{id}/approve  — approve pending operation
 *   POST /runs/{id}/reject   — reject pending operation
 */

'use strict';

// ── Pipeline node names in execution order ───────────────────
const PIPELINE_NODES = [
  'ingest',
  'policy_gate',
  'context_builder',
  'router',
  'planner',
  'coder',
  'executor',
  'verifier',
  'reporter',
];

// ── Application state ─────────────────────────────────────────
/** The run_id currently displayed in the console, or null. */
let _selectedRunId = null;

/** The active EventSource connection, or null. */
let _activeSSE = null;

/** Set of node names that have completed in the current run. */
let _completedNodes = new Set();

/** The name of the currently active (pulsing) node, or null. */
let _activeNode = null;

/** setInterval handle for the 5-second run-list refresh. */
let _listTimer = null;

// ── HTML escaping ─────────────────────────────────────────────

/**
 * Escape a value for safe insertion into HTML attribute or content.
 * @param {unknown} s
 * @returns {string}
 */
function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Time formatting ───────────────────────────────────────────

/**
 * Format an ISO-8601 timestamp as a locale time string (HH:MM:SS).
 * @param {string|null|undefined} iso
 * @returns {string}
 */
function fmtTime(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleTimeString();
  } catch (_) {
    return iso;
  }
}

// ── Status helpers ────────────────────────────────────────────

/**
 * Map a run status string to one of the CSS badge class suffixes.
 * @param {string} s
 * @returns {string}
 */
function statusClass(s) {
  if (s === 'running' || s === 'starting')  return 'running';
  if (s === 'succeeded')                    return 'succeeded';
  if (s === 'failed')                       return 'failed';
  if (s === 'suspended')                    return 'suspended';
  if (s === 'cancelled')                    return 'cancelled';
  if (s === 'cancelling')                   return 'cancelling';
  return 'other';
}

/**
 * Return true if the run status indicates the run is still progressing.
 * @param {string} s
 * @returns {boolean}
 */
function isActive(s) {
  return s === 'running' || s === 'starting' || s === 'cancelling';
}

// ── Run history sidebar ───────────────────────────────────────

/**
 * Fetch the run list from /v1/runs and re-render the sidebar.
 * Called on load and every 5 seconds by _listTimer.
 */
function refreshRunList() {
  fetch('/v1/runs')
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (data && Array.isArray(data.runs)) {
        renderRunList(data.runs);
      }
    })
    .catch(() => {});
}

/**
 * Render the run history list in the sidebar.
 * @param {Array<object>} runs  Run summary objects from the API.
 */
function renderRunList(runs) {
  const el = document.getElementById('run-list');
  if (!el) return;

  // Show most recent first
  const sorted = [...runs].sort((a, b) =>
    (b.created_at || '').localeCompare(a.created_at || '')
  );

  el.innerHTML = sorted
    .map((r) => {
      const idSnip = String(r.run_id || '').slice(0, 8);
      const sel = r.run_id === _selectedRunId ? ' active' : '';
      const cls = statusClass(r.status);
      const ts = fmtTime(r.created_at);
      return (
        `<div class="run-item${sel}" onclick="selectRun(${JSON.stringify(r.run_id)})">` +
        `  <div class="run-item-id">${esc(idSnip)}&hellip;</div>` +
        `  <div class="run-item-meta">` +
        `    <span class="badge badge-${cls}">${esc(r.status)}</span>` +
        `    <span class="run-item-ts">${esc(ts)}</span>` +
        `  </div>` +
        `</div>`
      );
    })
    .join('');
}

// ── Run selection & SSE stream ────────────────────────────────

/**
 * Select a run and open an SSE stream to display live events.
 * Exposed as window.selectRun for onclick handlers.
 * @param {string} runId
 */
function selectRun(runId) {
  _selectedRunId = runId;

  // Tear down any existing stream
  if (_activeSSE) {
    _activeSSE.close();
    _activeSSE = null;
  }

  // Reset console state
  _completedNodes = new Set();
  _activeNode = null;
  clearEventLog();
  resetNodeGraph();
  hideBanner();
  hideCompleteBanner();
  updateStatusBadge('connecting…');

  // Re-render sidebar to highlight selected item
  refreshRunList();

  // Hide empty state, show panels
  const empty = document.getElementById('empty-state');
  if (empty) empty.style.display = 'none';
  const logPanel = document.getElementById('event-log-panel');
  if (logPanel) logPanel.style.display = 'flex';
  const graphPanel = document.getElementById('graph-panel');
  if (graphPanel) graphPanel.style.display = 'flex';

  // Open SSE stream to the new Wave-7 endpoint
  const url = '/runs/' + encodeURIComponent(runId) + '/stream';
  const es = new EventSource(url);
  _activeSSE = es;

  es.onmessage = (ev) => {
    try {
      const event = JSON.parse(ev.data);
      handleSSEEvent(event);
    } catch (_) {}
  };

  es.onerror = () => {
    es.close();
    _activeSSE = null;
    showSpinner(false);
    updateStatusBadge('disconnected');
  };
}

/**
 * Route a single parsed SSE event to the appropriate handler.
 * @param {object} event  Parsed JSON from the SSE data frame.
 */
function handleSSEEvent(event) {
  // ── Done sentinel — stream is complete ──────────────────
  if (event.type === 'done') {
    if (_activeSSE) {
      _activeSSE.close();
      _activeSSE = null;
    }
    showSpinner(false);
    showCompleteBanner();
    // Deactivate the last node if still pulsing
    if (_activeNode) {
      const el = document.getElementById('node-' + _activeNode);
      if (el) { el.classList.remove('active'); el.classList.add('done'); }
      _activeNode = null;
    }
    refreshRunList();
    return;
  }

  // ── Server-side error ────────────────────────────────────
  if (event.error) {
    appendEventRow({
      kind: 'error',
      ts_ms: Date.now(),
      data: { message: event.error },
    });
    showSpinner(false);
    return;
  }

  // ── Approval requested ───────────────────────────────────
  if (event.type === 'approval_requested' || event.kind === 'approval_requested') {
    const summary =
      event.summary ||
      (event.data && event.data.summary) ||
      'Approval required';
    showApprovalBanner(summary);
    return;
  }

  // ── Full run payload (replayed for completed runs) ───────
  // Identified by having both `status` and `run_id` keys.
  if ('status' in event && 'run_id' in event) {
    updateStatusBadge(event.status || '?');
    if (event.pending_approval) {
      showApprovalBanner(
        event.pending_approval_summary || 'Approval required'
      );
    }
    return;
  }

  // ── Normal trace event: { ts_ms, kind, data } ───────────
  if (event.kind) {
    appendEventRow(event);
    handleNodeEvent(event);
    showSpinner(true);
    updateStatusBadge('running');
  }
}

// ── Event log ─────────────────────────────────────────────────

/**
 * Append a colour-coded row to the event log and auto-scroll.
 * @param {object} event  Trace event with `kind`, `ts_ms`, and `data`.
 */
function appendEventRow(event) {
  const log = document.getElementById('event-log');
  if (!log) return;

  const kind = String(event.kind || 'event');
  const tsMs = event.ts_ms || Date.now();
  // Use HH:MM:SS from the event timestamp
  const ts = new Date(tsMs).toISOString().slice(11, 19);
  const data = event.data || {};

  // Build a short human-readable detail string
  let detail = '';
  if (data.name)    detail = data.name;
  else if (data.text)   detail = String(data.text).slice(0, 120);
  else if (data.message) detail = String(data.message).slice(0, 120);
  else if (data.tool)   detail = data.tool;

  // Map kind to a safe CSS class name (replace unsafe chars with _)
  const kindCls = kind.replace(/[^a-z_]/g, '_');

  const row = document.createElement('div');
  row.className = `event-row event-${kindCls}`;
  row.innerHTML =
    `<span class="ev-ts">${esc(ts)}</span>` +
    `<span class="ev-kind">${esc(kind)}</span>` +
    `<span class="ev-detail">${esc(detail)}</span>`;

  log.appendChild(row);

  // Auto-scroll to bottom
  log.scrollTop = log.scrollHeight;
}

/** Remove all rows from the event log. */
function clearEventLog() {
  const log = document.getElementById('event-log');
  if (log) log.innerHTML = '';
}

// ── Spinner & status badge ────────────────────────────────────

/**
 * Show or hide the pulsing live-indicator dot.
 * @param {boolean} on
 */
function showSpinner(on) {
  const el = document.getElementById('event-spinner');
  if (el) el.classList.toggle('active', on);
}

/**
 * Update the status text in the header badge.
 * @param {string} status
 */
function updateStatusBadge(status) {
  const el = document.getElementById('run-status-badge');
  if (el) el.textContent = status;
}

// ── Complete banner ───────────────────────────────────────────

/** Show the "Run complete ✓" banner at the bottom of the event log. */
function showCompleteBanner() {
  const el = document.getElementById('run-complete-banner');
  if (el) el.classList.add('visible');
}

/** Hide the complete banner (called when a new run is selected). */
function hideCompleteBanner() {
  const el = document.getElementById('run-complete-banner');
  if (el) el.classList.remove('visible');
}

// ── Node graph ────────────────────────────────────────────────

/**
 * Inject the horizontal pipeline graph into #node-graph.
 * Creates one .pipeline-node div per node name in PIPELINE_NODES.
 */
function buildNodeGraph() {
  const el = document.getElementById('node-graph');
  if (!el) return;

  el.innerHTML = PIPELINE_NODES.map((name, i) =>
    `<div class="pipeline-node">` +
    (i > 0 ? `<span class="pipe-arrow">&rarr;</span>` : '') +
    `<div class="node-box" id="node-${esc(name)}">` +
    `${esc(name)}<span class="node-check"> &#x2713;</span>` +
    `</div>` +
    `</div>`
  ).join('');
}

/** Remove all active/done CSS classes from every pipeline node box. */
function resetNodeGraph() {
  PIPELINE_NODES.forEach((name) => {
    const el = document.getElementById('node-' + name);
    if (el) {
      el.classList.remove('active', 'done');
    }
  });
}

/**
 * Handle `node_start` and `node_end` trace events to animate the pipeline.
 * @param {object} event  Trace event.
 */
function handleNodeEvent(event) {
  const kind = String(event.kind || '');
  const data = event.data || {};
  const nodeName = String(data.name || '').toLowerCase();

  // Only handle events for known pipeline nodes
  if (!nodeName || !PIPELINE_NODES.includes(nodeName)) return;

  if (kind === 'node_start') {
    // Deactivate the previously active node (without marking it done)
    if (_activeNode && _activeNode !== nodeName) {
      const prev = document.getElementById('node-' + _activeNode);
      if (prev) prev.classList.remove('active');
    }
    _activeNode = nodeName;
    const el = document.getElementById('node-' + nodeName);
    if (el) {
      el.classList.remove('done');
      el.classList.add('active');
    }
  } else if (kind === 'node_end') {
    const el = document.getElementById('node-' + nodeName);
    if (el) {
      el.classList.remove('active');
      el.classList.add('done');
    }
    _completedNodes.add(nodeName);
    if (_activeNode === nodeName) _activeNode = null;
  }
}

// ── Approval banner ───────────────────────────────────────────

/**
 * Show the approval banner with a summary message.
 * @param {string} summary  Human-readable description of what needs approval.
 */
function showApprovalBanner(summary) {
  const banner = document.getElementById('approval-banner');
  const text = document.getElementById('approval-text');
  if (banner) banner.classList.add('visible');
  if (text) text.textContent = '\u26A0 ' + summary;
}

/** Hide the approval banner. */
function hideBanner() {
  const el = document.getElementById('approval-banner');
  if (el) el.classList.remove('visible');
}

/**
 * Approve the pending operation for the selected run.
 * Calls POST /runs/{id}/approve and reopens the SSE stream.
 * Exposed as window.approveRun for onclick handlers.
 */
function approveRun() {
  if (!_selectedRunId) return;
  fetch('/runs/' + encodeURIComponent(_selectedRunId) + '/approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ actor: 'spa' }),
  })
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      hideBanner();
      if (data && data.run_id) {
        // Reopen stream for the resumed run
        selectRun(data.run_id);
      }
    })
    .catch(() => {});
}

/**
 * Reject the pending operation for the selected run.
 * Calls POST /runs/{id}/reject and refreshes the sidebar.
 * Exposed as window.rejectRun for onclick handlers.
 */
function rejectRun() {
  if (!_selectedRunId) return;
  fetch('/runs/' + encodeURIComponent(_selectedRunId) + '/reject', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ actor: 'spa' }),
  })
    .then((r) => (r.ok ? r.json() : null))
    .then(() => {
      hideBanner();
      refreshRunList();
      updateStatusBadge('rejected');
    })
    .catch(() => {});
}

// ── Bootstrap ─────────────────────────────────────────────────

/**
 * Initialise the SPA: build the node graph, load runs, start polling.
 * Called once the DOM is ready.
 */
function init() {
  buildNodeGraph();

  // Hide panels until a run is selected
  const logPanel = document.getElementById('event-log-panel');
  if (logPanel) logPanel.style.display = 'none';
  const graphPanel = document.getElementById('graph-panel');
  if (graphPanel) graphPanel.style.display = 'none';

  refreshRunList();

  // Auto-refresh run list every 5 seconds
  _listTimer = setInterval(refreshRunList, 5000);
}

// Expose functions needed by inline onclick="" attributes in index.html
window.selectRun   = selectRun;
window.approveRun  = approveRun;
window.rejectRun   = rejectRun;

// Start when the DOM is ready (supports both deferred and inline script)
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
