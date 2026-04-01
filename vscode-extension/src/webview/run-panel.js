(function() {
    const vscode = acquireVsCodeApi();

    // State
    let nodes = {};
    let expandedNodes = new Set();

    // Listen for messages from extension host
    window.addEventListener('message', event => {
        const msg = event.data;
        switch(msg.type) {
            case 'run-state':
                renderRunHeader(msg.data);
                break;
            case 'sse-event':
                handleSseEvent(msg.data);
                break;
            case 'approval-requested':
                showApprovalBanner(msg.data);
                break;
            case 'run-done':
                markDone();
                break;
            case 'error':
                showError(msg.message);
                break;
        }
    });

    // Approve/Reject buttons
    document.getElementById('btn-approve').addEventListener('click', () => {
        const banner = document.getElementById('approval-banner');
        const challengeId = banner.dataset.challengeId || '';
        vscode.postMessage({ type: 'approve', challenge_id: challengeId });
        banner.classList.add('hidden');
    });

    document.getElementById('btn-reject').addEventListener('click', () => {
        vscode.postMessage({ type: 'reject' });
        document.getElementById('approval-banner').classList.add('hidden');
    });

    function renderRunHeader(run) {
        document.getElementById('run-title').textContent = run.request || run.task || '';
        document.getElementById('run-status').textContent = (run.status || '').toUpperCase();
        document.getElementById('run-status').className = 'status-badge status-' + (run.status || 'unknown');
        if (run.elapsed_ms) {
            const s = (run.elapsed_ms / 1000).toFixed(1);
            document.getElementById('run-elapsed').textContent = s + 's';
        }
        renderPipeline(run.current_node);
    }

    const STAGES = ['ingest','router','planner','coder','executor','verifier','reporter'];

    function renderPipeline(currentNode) {
        const bar = document.getElementById('pipeline-bar');
        bar.innerHTML = '';
        const idx = STAGES.indexOf(currentNode);
        STAGES.forEach((stage, i) => {
            const seg = document.createElement('div');
            seg.className = 'pipeline-segment';
            if (i < idx) seg.classList.add('done');
            else if (i === idx) seg.classList.add('active');
            bar.appendChild(seg);
        });
    }

    function handleSseEvent(data) {
        // Parse as typed event
        if (data.type === 'tool_stdout') {
            appendStdout(data.tool, data.line);
            return;
        }
        if (data.type === 'final_output') {
            showFinalOutput(data.text);
            return;
        }
        if (data.type === 'approval_requested') {
            showApprovalBanner(data);
            return;
        }

        // Trace event
        const node = data.node || 'unknown';
        const kind = data.kind || '';

        if (!nodes[node]) {
            nodes[node] = { events: [], done: false };
            createNodeSection(node);
        }
        nodes[node].events.push(data);
        if (kind === 'node_end') nodes[node].done = true;

        updateNodeSection(node);

        // Check for diffs
        if (kind === 'tool_result' && data.data) {
            const tool = data.data.tool;
            if (tool === 'apply_patch' || tool === 'write_file') {
                appendDiff(tool, data.data.stdout || '');
            }
        }
    }

    function createNodeSection(name) {
        const stream = document.getElementById('event-stream');
        const section = document.createElement('div');
        section.className = 'node-section';
        section.id = 'node-' + name;
        section.innerHTML =
            '<div class="node-header" onclick="toggleNode(\'' + name + '\')">' +
                '<span class="node-arrow">&#9656;</span>' +
                '<span class="node-name">' + escapeHtml(name) + '</span>' +
                '<span class="node-status">&#9679;</span>' +
                '<span class="node-meta"></span>' +
            '</div>' +
            '<div class="node-detail hidden"></div>';
        stream.appendChild(section);
    }

    // Make toggleNode global for onclick
    window.toggleNode = function(name) {
        const section = document.getElementById('node-' + name);
        const detail = section.querySelector('.node-detail');
        const arrow = section.querySelector('.node-arrow');
        if (expandedNodes.has(name)) {
            expandedNodes.delete(name);
            detail.classList.add('hidden');
            arrow.textContent = '\u25B8';
        } else {
            expandedNodes.add(name);
            detail.classList.remove('hidden');
            arrow.textContent = '\u25BE';
            renderNodeDetail(name);
        }
    };

    function updateNodeSection(name) {
        const section = document.getElementById('node-' + name);
        if (!section) return;
        var info = nodes[name];
        var status = section.querySelector('.node-status');
        status.textContent = info.done ? '\u2713' : '\u25CF';
        status.className = 'node-status ' + (info.done ? 'done' : 'active');

        var toolCount = info.events.filter(function(e) {
            return e.kind === 'tool_call' || e.kind === 'tool_result';
        }).length;
        section.querySelector('.node-meta').textContent = toolCount + ' tools';

        if (expandedNodes.has(name)) renderNodeDetail(name);
    }

    function renderNodeDetail(name) {
        const section = document.getElementById('node-' + name);
        const detail = section.querySelector('.node-detail');
        detail.innerHTML = nodes[name].events.map(function(ev) {
            const kind = ev.kind || '';
            const cls = kind === 'error' ? 'ev-error' : kind.startsWith('tool') ? 'ev-tool' : 'ev-default';
            const text = ev.data ? (typeof ev.data === 'string' ? ev.data : JSON.stringify(ev.data).slice(0, 200)) : '';
            return '<div class="event-line ' + cls + '">[' + kind + '] ' + escapeHtml(text) + '</div>';
        }).join('');
    }

    function appendStdout(tool, line) {
        const stream = document.getElementById('event-stream');
        const el = document.createElement('div');
        el.className = 'stdout-line';
        el.textContent = '[' + tool + '] ' + line;
        stream.appendChild(el);
        stream.scrollTop = stream.scrollHeight;
    }

    function showApprovalBanner(data) {
        const banner = document.getElementById('approval-banner');
        banner.classList.remove('hidden');
        banner.dataset.challengeId = data.challenge_id || '';
        document.getElementById('approval-text').textContent =
            'Approval Required: ' + (data.summary || data.operation_class || 'Unknown operation');
    }

    function showFinalOutput(text) {
        const el = document.getElementById('final-output');
        el.classList.remove('hidden');
        el.innerHTML = '<div class="label">FINAL OUTPUT</div><pre>' + escapeHtml(text) + '</pre>';
    }

    function appendDiff(tool, content) {
        const panel = document.getElementById('diff-panel');
        panel.classList.remove('hidden');
        const block = document.createElement('div');
        block.className = 'diff-block';
        block.innerHTML = '<div class="label">' + escapeHtml(tool) + '</div><pre>' +
            content.split('\n').map(function(line) {
                const cls = line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-del' : line.startsWith('@@') ? 'diff-hunk' : '';
                return '<span class="' + cls + '">' + escapeHtml(line) + '</span>';
            }).join('\n') + '</pre>';
        panel.appendChild(block);
    }

    function markDone() {
        const status = document.getElementById('run-status');
        if (status.textContent === 'RUNNING') {
            status.textContent = 'COMPLETED';
            status.className = 'status-badge status-completed';
        }
    }

    function showError(msg) {
        const stream = document.getElementById('event-stream');
        const el = document.createElement('div');
        el.className = 'error-msg';
        el.textContent = msg;
        stream.appendChild(el);
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
})();
