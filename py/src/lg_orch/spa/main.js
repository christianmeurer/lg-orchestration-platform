/**
 * main.js — LG Orchestration Platform Live Console
 *
 * Vanilla JS, no build step, no npm.
 * Talks to the ThreadingHTTPServer in remote_api.py via:
 *   GET  /v1/runs            — run history list
 *   GET  /runs/{id}/stream   — SSE live event stream (Wave 7 endpoint)
 *   POST /runs/{id}/approve  — approve pending operation
 *   POST /runs/{id}/reject   — reject pending operation
 *
 * Wave 7: D3 v7 force-directed graph visualisation of agent nodes.
 */

'use strict';

// ── Pipeline node names ───────────────────────────────────────
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

// ── Graph topology ────────────────────────────────────────────
const GRAPH_LINKS = [
  { source: 'ingest',          target: 'policy_gate',     retry: false },
  { source: 'policy_gate',     target: 'context_builder', retry: false },
  { source: 'policy_gate',     target: 'reporter',        retry: false },
  { source: 'context_builder', target: 'router',          retry: false },
  { source: 'router',          target: 'planner',         retry: false },
  { source: 'planner',         target: 'coder',           retry: false },
  { source: 'coder',           target: 'executor',        retry: false },
  { source: 'executor',        target: 'verifier',        retry: false },
  { source: 'verifier',        target: 'reporter',        retry: false },
  { source: 'verifier',        target: 'policy_gate',     retry: true  },
];

// ── Node visual state colours ─────────────────────────────────
const NODE_COLORS = {
  idle:   { fill: '#1c2128', stroke: '#444' },
  active: { fill: '#1f6feb', stroke: '#58a6ff' },
  done:   { fill: '#238636', stroke: '#3fb950' },
  error:  { fill: '#b62324', stroke: '#f85149' },
};

// ── Application state ─────────────────────────────────────────
let _selectedRunId = null;
let _activeSSE = null;
let _completedNodes = new Set();
let _activeNode = null;
let _listTimer = null;

/** Per-node llm-stream <pre> elements: node name → HTMLElement */
let _llmStreamEls = {};

/** Per-node state: 'idle' | 'active' | 'done' | 'error' */
let _nodeStates = {};

/** D3 simulation instance (kept for resize). */
let _simulation = null;

/** D3 selection of node circles (for state updates). */
let _nodeSelection = null;

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

  if (_activeSSE) {
    _activeSSE.close();
    _activeSSE = null;
  }

  _completedNodes = new Set();
  _activeNode = null;
  _llmStreamEls = {};
  clearEventLog();
  clearLlmStreams();
  resetNodeGraph();
  hideBanner();
  hideCompleteBanner();
  updateStatusBadge('connecting\u2026');

  refreshRunList();

  const empty = document.getElementById('empty-state');
  if (empty) empty.style.display = 'none';
  const logPanel = document.getElementById('event-log-panel');
  if (logPanel) logPanel.style.display = 'flex';
  const graphPanel = document.getElementById('graph-panel');
  if (graphPanel) graphPanel.style.display = 'flex';

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
  if (event.type === 'done') {
    if (_activeSSE) {
      _activeSSE.close();
      _activeSSE = null;
    }
    showSpinner(false);
    showCompleteBanner();
    markLlmStreamsComplete();
    if (_activeNode) {
      setNodeState(_activeNode, 'done');
      _activeNode = null;
    }
    refreshRunList();
    return;
  }

  if (event.error) {
    appendEventRow({
      kind: 'error',
      ts_ms: Date.now(),
      data: { message: event.error },
    });
    showSpinner(false);
    markLlmStreamsComplete();
    return;
  }

  if (event.type === 'approval_requested' || event.kind === 'approval_requested') {
    const summary =
      event.summary ||
      (event.data && event.data.summary) ||
      'Approval required';
    showApprovalBanner(summary);
    return;
  }

  if (event.type === 'llm_chunk') {
    handleLlmChunk(event);
    return;
  }

  if ('status' in event && 'run_id' in event) {
    updateStatusBadge(event.status || '?');
    if (event.pending_approval) {
      showApprovalBanner(
        event.pending_approval_summary || 'Approval required'
      );
    }
    return;
  }

  if (event.kind) {
    appendEventRow(event);
    handleNodeEvent(event);
    showSpinner(true);
    updateStatusBadge('running');
  }
}

// ── LLM streaming chunks ──────────────────────────────────────

/**
 * Find or create a <pre class="llm-stream"> element for a given node name.
 * Inserts it into #event-log so it appears inline with other events.
 * @param {string} nodeName
 * @returns {HTMLElement}
 */
function _getOrCreateLlmStreamEl(nodeName) {
  if (_llmStreamEls[nodeName]) {
    return _llmStreamEls[nodeName];
  }
  const log = document.getElementById('event-log');
  if (!log) {
    const fallback = document.createElement('pre');
    _llmStreamEls[nodeName] = fallback;
    return fallback;
  }
  const pre = document.createElement('pre');
  pre.className = 'llm-stream';
  pre.setAttribute('data-node', nodeName);
  log.appendChild(pre);
  _llmStreamEls[nodeName] = pre;
  return pre;
}

/**
 * Handle a parsed llm_chunk SSE event: append delta to the node's stream area.
 * @param {object} event  Object with `node` and `delta` fields.
 */
function handleLlmChunk(event) {
  const nodeName = String(event.node || '');
  const delta = String(event.delta || '');
  if (!nodeName || !delta) return;

  const el = _getOrCreateLlmStreamEl(nodeName);
  if (el.getAttribute('data-complete') === 'true') return;

  el.textContent += delta;
  el.scrollTop = el.scrollHeight;
}

/** Mark all active llm-stream elements as complete (run finished/errored). */
function markLlmStreamsComplete() {
  const log = document.getElementById('event-log');
  if (!log) return;
  const streamEls = log.querySelectorAll('.llm-stream:not([data-complete="true"])');
  streamEls.forEach((el) => {
    el.setAttribute('data-complete', 'true');
  });
  // Also update any tracked refs
  Object.values(_llmStreamEls).forEach((el) => {
    el.setAttribute('data-complete', 'true');
  });
}

/** Remove all llm-stream elements from the log (called on run change). */
function clearLlmStreams() {
  const log = document.getElementById('event-log');
  if (log) {
    const streamEls = log.querySelectorAll('.llm-stream');
    streamEls.forEach((el) => el.remove());
  }
  _llmStreamEls = {};
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
  const ts = new Date(tsMs).toISOString().slice(11, 19);
  const data = event.data || {};

  let detail = '';
  if (data.name)         detail = data.name;
  else if (data.text)    detail = String(data.text).slice(0, 120);
  else if (data.message) detail = String(data.message).slice(0, 120);
  else if (data.tool)    detail = data.tool;

  const kindCls = kind.replace(/[^a-z_]/g, '_');

  const row = document.createElement('div');
  row.className = `event-row event-${kindCls}`;
  row.innerHTML =
    `<span class="ev-ts">${esc(ts)}</span>` +
    `<span class="ev-kind">${esc(kind)}</span>` +
    `<span class="ev-detail">${esc(detail)}</span>`;

  log.appendChild(row);
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

function showCompleteBanner() {
  const el = document.getElementById('run-complete-banner');
  if (el) el.classList.add('visible');
}

function hideCompleteBanner() {
  const el = document.getElementById('run-complete-banner');
  if (el) el.classList.remove('visible');
}

// ── D3 Force Graph ────────────────────────────────────────────

/**
 * Initialise per-node state to idle.
 */
function _initNodeStates() {
  _nodeStates = {};
  PIPELINE_NODES.forEach((n) => { _nodeStates[n] = 'idle'; });
}

/**
 * Set a single node's state and redraw its visual representation.
 * @param {string} name
 * @param {'idle'|'active'|'done'|'error'} state
 */
function setNodeState(name, state) {
  if (!(name in _nodeStates)) return;
  _nodeStates[name] = state;
  _applyNodeVisuals();
}

/**
 * Push current _nodeStates onto the SVG circles and their filters.
 */
function _applyNodeVisuals() {
  if (!_nodeSelection) return;
  _nodeSelection
    .attr('fill', (d) => NODE_COLORS[_nodeStates[d.id] || 'idle'].fill)
    .attr('stroke', (d) => NODE_COLORS[_nodeStates[d.id] || 'idle'].stroke)
    .attr('class', (d) => {
      const st = _nodeStates[d.id] || 'idle';
      return st === 'active' ? 'graph-node graph-node-active' : 'graph-node';
    });
}

/**
 * Build the D3 v7 force-directed graph inside #graph-container.
 * Safe to call multiple times — clears the container first.
 */
function buildNodeGraph() {
  const container = document.getElementById('graph-container');
  if (!container) return;

  // Clear previous SVG if any
  container.innerHTML = '';

  _initNodeStates();

  const W = container.clientWidth  || 600;
  const H = container.clientHeight || 500;

  // ── SVG root ─────────────────────────────────────────────────
  const svg = d3.select(container)
    .append('svg')
    .attr('width', '100%')
    .attr('height', '100%')
    .attr('viewBox', `0 0 ${W} ${H}`)
    .style('display', 'block');

  // ── Defs: arrow markers + glow filter ───────────────────────
  const defs = svg.append('defs');

  // Arrow for regular edges
  defs.append('marker')
    .attr('id', 'arrow-regular')
    .attr('viewBox', '0 -5 10 10')
    .attr('refX', 32)
    .attr('refY', 0)
    .attr('markerWidth', 6)
    .attr('markerHeight', 6)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M0,-5L10,0L0,5')
    .attr('fill', '#30363d');

  // Arrow for retry edge
  defs.append('marker')
    .attr('id', 'arrow-retry')
    .attr('viewBox', '0 -5 10 10')
    .attr('refX', 32)
    .attr('refY', 0)
    .attr('markerWidth', 6)
    .attr('markerHeight', 6)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M0,-5L10,0L0,5')
    .attr('fill', '#f0883e');

  // Glow filter for active nodes
  const glowFilter = defs.append('filter')
    .attr('id', 'glow-active')
    .attr('x', '-50%')
    .attr('y', '-50%')
    .attr('width', '200%')
    .attr('height', '200%');
  glowFilter.append('feGaussianBlur')
    .attr('stdDeviation', '4')
    .attr('result', 'blur');
  const feMerge = glowFilter.append('feMerge');
  feMerge.append('feMergeNode').attr('in', 'blur');
  feMerge.append('feMergeNode').attr('in', 'SourceGraphic');

  // ── Graph data ───────────────────────────────────────────────
  const nodes = PIPELINE_NODES.map((id) => ({ id }));
  const links = GRAPH_LINKS.map((l) => ({ ...l }));

  // ── Force simulation ─────────────────────────────────────────
  _simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id((d) => d.id).distance(120))
    .force('charge', d3.forceManyBody().strength(-400))
    .force('center', d3.forceCenter(W / 2, H / 2))
    .force('collide', d3.forceCollide(40));

  // ── Link layer ───────────────────────────────────────────────
  const link = svg.append('g')
    .attr('class', 'links')
    .selectAll('line')
    .data(links)
    .join('line')
    .attr('stroke', (d) => d.retry ? '#f0883e' : '#30363d')
    .attr('stroke-width', 1.5)
    .attr('stroke-dasharray', (d) => d.retry ? '5,3' : null)
    .attr('marker-end', (d) => d.retry ? 'url(#arrow-retry)' : 'url(#arrow-regular)');

  // ── Node layer ───────────────────────────────────────────────
  const nodeGroup = svg.append('g')
    .attr('class', 'nodes')
    .selectAll('g')
    .data(nodes)
    .join('g')
    .call(
      d3.drag()
        .on('start', (event, d) => {
          if (!event.active) _simulation.alphaTarget(0.3).restart();
          d.fx = d.x;
          d.fy = d.y;
        })
        .on('drag', (event, d) => {
          d.fx = event.x;
          d.fy = event.y;
        })
        .on('end', (event, d) => {
          if (!event.active) _simulation.alphaTarget(0);
          d.fx = null;
          d.fy = null;
        })
    );

  // Circle
  _nodeSelection = nodeGroup.append('circle')
    .attr('r', 22)
    .attr('class', 'graph-node')
    .attr('fill', NODE_COLORS.idle.fill)
    .attr('stroke', NODE_COLORS.idle.stroke)
    .attr('stroke-width', 2);

  // Label
  nodeGroup.append('text')
    .attr('text-anchor', 'middle')
    .attr('dy', 36)
    .attr('font-size', 11)
    .attr('font-family', 'sans-serif')
    .attr('fill', '#8b949e')
    .text((d) => d.id);

  // ── Tick handler ─────────────────────────────────────────────
  _simulation.on('tick', () => {
    link
      .attr('x1', (d) => d.source.x)
      .attr('y1', (d) => d.source.y)
      .attr('x2', (d) => d.target.x)
      .attr('y2', (d) => d.target.y);

    nodeGroup.attr('transform', (d) => `translate(${d.x},${d.y})`);
  });

  // ── Responsive resize ─────────────────────────────────────────
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => {
      const nw = container.clientWidth  || 600;
      const nh = container.clientHeight || 500;
      svg.attr('viewBox', `0 0 ${nw} ${nh}`);
      if (_simulation) {
        _simulation.force('center', d3.forceCenter(nw / 2, nh / 2));
        _simulation.alpha(0.3).restart();
      }
    });
    ro.observe(container);
  }
}

/** Reset all nodes to idle state. */
function resetNodeGraph() {
  _initNodeStates();
  _applyNodeVisuals();
}

/**
 * Handle `node_start` and `node_end` trace events to animate the graph.
 * @param {object} event  Trace event.
 */
function handleNodeEvent(event) {
  const kind = String(event.kind || '');
  const data = event.data || {};
  const nodeName = String(data.name || '').toLowerCase();

  if (!nodeName || !PIPELINE_NODES.includes(nodeName)) return;

  if (kind === 'node_start') {
    if (_activeNode && _activeNode !== nodeName) {
      // Leave previous node in its current done/error state if already set,
      // otherwise drop back to idle.
      if (_nodeStates[_activeNode] === 'active') {
        setNodeState(_activeNode, 'idle');
      }
    }
    _activeNode = nodeName;
    setNodeState(nodeName, 'active');
  } else if (kind === 'node_end') {
    const ok = data.ok !== false;
    setNodeState(nodeName, ok ? 'done' : 'error');
    _completedNodes.add(nodeName);
    if (_activeNode === nodeName) _activeNode = null;
  }
}

// ── Approval banner ───────────────────────────────────────────

/**
 * Show the approval banner with a summary message.
 * @param {string} summary
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
        selectRun(data.run_id);
      }
    })
    .catch(() => {});
}

/**
 * Reject the pending operation for the selected run.
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
 * Initialise the SPA: build the D3 force graph, load runs, start polling.
 */
function init() {
  buildNodeGraph();

  const logPanel = document.getElementById('event-log-panel');
  if (logPanel) logPanel.style.display = 'none';
  const graphPanel = document.getElementById('graph-panel');
  if (graphPanel) graphPanel.style.display = 'none';

  refreshRunList();
  _listTimer = setInterval(refreshRunList, 5000);
}

window.selectRun  = selectRun;
window.approveRun = approveRun;
window.rejectRun  = rejectRun;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
