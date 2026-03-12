import { spawn, type ChildProcess } from 'child_process';
import * as http from 'http';
import * as https from 'https';
import * as fs from 'fs/promises';
import * as path from 'path';
import * as vscode from 'vscode';

const RUNNER_BIND = '127.0.0.1:8088';
const RUNNER_BASE_URL = 'http://127.0.0.1:8088';
const RUNNER_API_KEY = 'dev-insecure';
const DEFAULT_REMOTE_POLL_INTERVAL_MS = 1000;
const LOG_LIMIT = 1000;

const COMMANDS = {
  openPanel: 'lgOrch.openPanel',
  runRequest: 'lgOrch.runRequest',
  startRunner: 'lgOrch.startRunner',
  stopRunner: 'lgOrch.stopRunner',
} as const;

interface TraceSummary {
  tracePath: string | null;
  finalOutput: string;
}

interface ViewModel {
  workspaceRoot: string;
  runnerStatus: string;
  requestStatus: string;
  latestTracePath: string;
  finalOutput: string;
  logs: string;
}

interface RemoteRunDetails {
  run_id?: unknown;
  status?: unknown;
  exit_code?: unknown;
  trace_path?: unknown;
  trace_ready?: unknown;
  trace?: unknown;
}

interface RemoteRunLogs {
  logs?: unknown;
}

class LgOrchExtension {
  private panel: vscode.WebviewPanel | undefined;
  private runnerProcess: ChildProcess | undefined;
  private readonly logLines: string[] = [];
  private latestTracePath: string | null = null;
  private latestFinalOutput = '';
  private runnerStatus = 'stopped';
  private requestStatus = 'idle';
  private requestRunning = false;

  public constructor(private readonly context: vscode.ExtensionContext) {}

  public register(): void {
    this.context.subscriptions.push(
      vscode.commands.registerCommand(COMMANDS.openPanel, async () => {
        await this.openPanel();
      }),
      vscode.commands.registerCommand(COMMANDS.startRunner, async () => {
        await this.startRunner();
      }),
      vscode.commands.registerCommand(COMMANDS.stopRunner, async () => {
        await this.stopRunner();
      }),
      vscode.commands.registerCommand(COMMANDS.runRequest, async (request: unknown) => {
        await this.runRequest(typeof request === 'string' ? request : undefined);
      }),
    );
  }

  public async dispose(): Promise<void> {
    await this.stopRunner(false);
    this.panel?.dispose();
    this.panel = undefined;
  }

  private async openPanel(): Promise<void> {
    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.One);
      this.refresh();
      return;
    }

    const panel = vscode.window.createWebviewPanel('lgOrch', 'Lula', vscode.ViewColumn.One, {
      enableScripts: true,
      retainContextWhenHidden: true,
    });

    panel.webview.html = this.renderHtml();
    panel.onDidDispose(() => {
      if (this.panel === panel) {
        this.panel = undefined;
      }
    });
    panel.webview.onDidReceiveMessage(async (message: unknown) => {
      await this.handleWebviewMessage(message);
    });

    this.panel = panel;
    this.refresh();
  }

  private async handleWebviewMessage(message: unknown): Promise<void> {
    if (!isRecord(message) || typeof message.type !== 'string') {
      return;
    }

    switch (message.type) {
      case 'runRequest': {
        const request = typeof message.request === 'string' ? message.request : undefined;
        await this.runRequest(request);
        return;
      }
      case 'startRunner':
        await this.startRunner();
        return;
      case 'stopRunner':
        await this.stopRunner();
        return;
      default:
        return;
    }
  }

  private async startRunner(): Promise<void> {
    await this.openPanel();

    if (this.runnerProcess && this.runnerProcess.exitCode === null) {
      this.appendLog('[runner] already running');
      this.runnerStatus = 'running';
      this.refresh();
      return;
    }

    const workspaceRoot = this.getWorkspaceRoot();
    if (!workspaceRoot) {
      return;
    }

    const cwd = path.join(workspaceRoot, 'rs');
    const args = [
      'run',
      '--',
      '--bind',
      RUNNER_BIND,
      '--root-dir',
      workspaceRoot,
      '--profile',
      'dev',
      '--api-key',
      RUNNER_API_KEY,
    ];

    this.runnerStatus = 'starting';
    this.appendLog(`[runner] starting: cargo ${args.join(' ')}`);

    const child = spawn('cargo', args, {
      cwd,
      detached: process.platform !== 'win32',
      env: process.env,
      shell: false,
    });

    this.runnerProcess = child;
    this.attachChildLogging(child, 'runner');

    child.on('spawn', () => {
      this.runnerStatus = 'running';
      this.appendLog('[runner] started');
      this.refresh();
    });

    child.on('error', (error: Error) => {
      if (this.runnerProcess === child) {
        this.runnerProcess = undefined;
      }
      this.runnerStatus = 'stopped';
      this.appendLog(`[runner] failed: ${error.message}`);
      this.refresh();
    });

    child.on('close', (code: number | null, signal: NodeJS.Signals | null) => {
      if (this.runnerProcess === child) {
        this.runnerProcess = undefined;
      }
      this.runnerStatus = 'stopped';
      this.appendLog(`[runner] exited code=${code ?? 'null'} signal=${signal ?? 'none'}`);
      this.refresh();
    });

    this.refresh();
  }

  private async stopRunner(logWhenMissing = true): Promise<void> {
    await this.openPanel();

    const child = this.runnerProcess;
    if (!child) {
      if (logWhenMissing) {
        this.appendLog('[runner] not running');
      }
      this.runnerStatus = 'stopped';
      this.refresh();
      return;
    }

    this.runnerStatus = 'stopping';
    this.appendLog('[runner] stopping');
    this.refresh();
    await this.terminateProcessTree(child);
  }

  private async runRequest(initialRequest?: string): Promise<void> {
    await this.openPanel();

    if (this.requestRunning) {
      this.appendLog('[run] request already in progress');
      return;
    }

    const workspaceRoot = this.getWorkspaceRoot();
    if (!workspaceRoot) {
      return;
    }

    const request = await this.resolveRequest(initialRequest);
    if (!request) {
      this.appendLog('[run] canceled');
      return;
    }

    this.requestRunning = true;
    this.requestStatus = 'starting';
    this.latestTracePath = null;
    this.latestFinalOutput = '';
    this.appendLog(`[run] request: ${request}`);
    this.refresh();

    try {
      const remoteApiBaseUrl = this.getRemoteApiBaseUrl();
      if (remoteApiBaseUrl) {
        await this.runRemoteRequest(request, remoteApiBaseUrl);
      } else {
        await this.runLocalRequest(request, workspaceRoot);
      }
    } catch (error: unknown) {
      this.requestStatus = 'failed';
      this.appendLog(`[run] failed: ${asErrorMessage(error)}`);
    } finally {
      this.requestRunning = false;
      this.refresh();
    }
  }

  private async runLocalRequest(request: string, workspaceRoot: string): Promise<void> {
    const pyDir = path.join(workspaceRoot, 'py');
    this.requestStatus = 'running';

    const syncCode = await this.runCommand('uv-sync', 'uv', ['sync'], pyDir);
    if (syncCode !== 0) {
      this.requestStatus = 'failed';
      this.appendLog('[run] uv sync failed');
      return;
    }

    const runArgs = [
      'run',
      'lg-orch',
      'run',
      request,
      '--trace',
      '--view',
      'classic',
      '--runner-base-url',
      RUNNER_BASE_URL,
    ];
    const runCode = await this.runCommand('cli', 'uv', runArgs, pyDir);
    const summary = await this.findLatestTrace(workspaceRoot);
    this.latestTracePath = summary.tracePath;
    this.latestFinalOutput = summary.finalOutput;

    if (summary.tracePath) {
      this.appendLog(`[trace] latest: ${summary.tracePath}`);
    } else {
      this.appendLog('[trace] no trace found');
    }

    if (runCode !== 0) {
      this.requestStatus = 'failed';
      this.appendLog('[run] command failed');
      return;
    }

    this.requestStatus = 'succeeded';
  }

  private async runRemoteRequest(request: string, remoteApiBaseUrl: string): Promise<void> {
    const pollIntervalMs = this.getRemotePollIntervalMs();
    this.appendLog(`[remote] using API: ${remoteApiBaseUrl}`);
    await this.requestJson('GET', `${remoteApiBaseUrl}/healthz`);
    this.appendLog('[remote] healthz ok');

    const created = await this.requestJson<RemoteRunDetails>('POST', `${remoteApiBaseUrl}/v1/runs`, {
      request,
      view: 'classic',
    });
    const runId = this.readRemoteRunId(created);
    this.appendLog(`[remote] run started: ${runId}`);
    this.applyRemoteRunDetails(created);

    let logCount = 0;
    while (true) {
      const detail = await this.requestJson<RemoteRunDetails>('GET', `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}`);
      this.applyRemoteRunDetails(detail);

      const logs = await this.requestJson<RemoteRunLogs>('GET', `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}/logs`);
      logCount = this.appendRemoteLogs(logs, logCount);

      if (!isRemoteRunInProgress(this.requestStatus)) {
        break;
      }

      await delay(pollIntervalMs);
    }

    const finalDetail = await this.requestJson<RemoteRunDetails>('GET', `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}`);
    this.applyRemoteRunDetails(finalDetail);
    this.appendRemoteLogs(
      await this.requestJson<RemoteRunLogs>('GET', `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}/logs`),
      logCount,
    );

    if (typeof finalDetail.exit_code === 'number') {
      this.appendLog(`[remote] completed exit_code=${finalDetail.exit_code}`);
    }
  }

  private async runCommand(label: string, command: string, args: readonly string[], cwd: string): Promise<number> {
    this.appendLog(`[${label}] ${command} ${args.join(' ')}`);

    const child = spawn(command, args, {
      cwd,
      env: process.env,
      shell: false,
    });

    this.attachChildLogging(child, label);

    return await new Promise<number>((resolve) => {
      let settled = false;
      const finish = (code: number): void => {
        if (settled) {
          return;
        }
        settled = true;
        resolve(code);
      };

      child.on('error', (error: Error) => {
        this.appendLog(`[${label}] failed: ${error.message}`);
        finish(-1);
      });

      child.on('close', (code: number | null, signal: NodeJS.Signals | null) => {
        this.appendLog(`[${label}] exited code=${code ?? 'null'} signal=${signal ?? 'none'}`);
        finish(code ?? -1);
      });
    });
  }

  private getRemoteApiBaseUrl(): string | null {
    const raw = vscode.workspace.getConfiguration('lula').get<string>('remoteApiBaseUrl', '').trim();
    if (!raw) {
      return null;
    }

    let parsed: URL;
    try {
      parsed = new URL(raw);
    } catch {
      throw new Error(`invalid lula.remoteApiBaseUrl: ${raw}`);
    }

    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      throw new Error(`invalid lula.remoteApiBaseUrl: ${raw}`);
    }

    return parsed.toString().replace(/\/+$/, '');
  }

  private getRemotePollIntervalMs(): number {
    const configured = vscode.workspace
      .getConfiguration('lula')
      .get<number>('remotePollIntervalMs', DEFAULT_REMOTE_POLL_INTERVAL_MS);
    if (!Number.isFinite(configured) || configured <= 0) {
      return DEFAULT_REMOTE_POLL_INTERVAL_MS;
    }
    return configured;
  }

  private applyRemoteRunDetails(detail: RemoteRunDetails): void {
    this.requestStatus = typeof detail.status === 'string' && detail.status.trim() ? detail.status : 'running';

    if (typeof detail.trace_path === 'string' && detail.trace_path.trim()) {
      this.latestTracePath = detail.trace_path;
    }

    if (detail.trace_ready === true && isRecord(detail.trace)) {
      this.latestFinalOutput = this.formatOutput(detail.trace.final);
    }

    this.refresh();
  }

  private appendRemoteLogs(payload: RemoteRunLogs, seenCount: number): number {
    const logs = Array.isArray(payload.logs) ? payload.logs : [];
    const startIndex = seenCount > logs.length ? 0 : seenCount;

    for (const line of logs.slice(startIndex)) {
      if (typeof line === 'string' && line.length > 0) {
        this.appendLog(`[remote:stdout] ${line}`);
      }
    }

    return logs.length;
  }

  private readRemoteRunId(detail: RemoteRunDetails): string {
    if (typeof detail.run_id === 'string' && detail.run_id.trim()) {
      return detail.run_id;
    }
    throw new Error('remote API response missing run_id');
  }

  private async requestJson<T>(method: 'GET' | 'POST', requestUrl: string, body?: unknown): Promise<T> {
    const url = new URL(requestUrl);
    const client = url.protocol === 'https:' ? https : url.protocol === 'http:' ? http : null;
    if (!client) {
      throw new Error(`unsupported protocol: ${url.protocol}`);
    }

    const payload = body === undefined ? undefined : JSON.stringify(body);

    return await new Promise<T>((resolve, reject) => {
      const request = client.request(
        {
          hostname: url.hostname,
          port: url.port,
          path: `${url.pathname}${url.search}`,
          method,
          headers:
            payload === undefined
              ? { Accept: 'application/json' }
              : {
                  Accept: 'application/json',
                  'Content-Type': 'application/json',
                  'Content-Length': Buffer.byteLength(payload).toString(),
                },
        },
        (response) => {
          const chunks: Buffer[] = [];
          response.on('data', (chunk: Buffer | string) => {
            chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
          });
          response.on('end', () => {
            const raw = Buffer.concat(chunks).toString('utf8');
            const statusCode = response.statusCode ?? 0;
            if (statusCode < 200 || statusCode >= 300) {
              reject(new Error(`HTTP ${statusCode}: ${raw || 'request failed'}`));
              return;
            }
            if (!raw.trim()) {
              resolve(undefined as T);
              return;
            }
            try {
              resolve(JSON.parse(raw) as T);
            } catch (error: unknown) {
              reject(new Error(`invalid JSON response: ${asErrorMessage(error)}`));
            }
          });
        },
      );

      request.setTimeout(30000, () => {
        request.destroy(new Error('request timed out'));
      });
      request.on('error', (error: Error) => reject(error));

      if (payload !== undefined) {
        request.write(payload);
      }
      request.end();
    });
  }

  private attachChildLogging(child: ChildProcess, label: string): void {
    this.attachStream(child.stdout, `${label}:stdout`);
    this.attachStream(child.stderr, `${label}:stderr`);
  }

  private attachStream(stream: NodeJS.ReadableStream | null | undefined, label: string): void {
    if (!stream) {
      return;
    }

    let buffered = '';

    stream.on('data', (chunk: Buffer | string) => {
      buffered += Buffer.isBuffer(chunk) ? chunk.toString('utf8') : chunk;
      const parts = buffered.split(/\r?\n/);
      buffered = parts.pop() ?? '';
      for (const part of parts) {
        if (part.length > 0) {
          this.appendLog(`[${label}] ${part}`);
        }
      }
    });

    stream.on('end', () => {
      if (buffered.length > 0) {
        this.appendLog(`[${label}] ${buffered}`);
      }
    });
  }

  private async terminateProcessTree(child: ChildProcess): Promise<void> {
    const pid = child.pid;
    if (pid === undefined) {
      child.kill();
      return;
    }

    if (process.platform === 'win32') {
      await new Promise<void>((resolve) => {
        const killer = spawn('taskkill', ['/pid', String(pid), '/t', '/f'], {
          env: process.env,
          shell: false,
        });
        killer.on('close', () => resolve());
        killer.on('error', () => {
          child.kill();
          resolve();
        });
      });
      return;
    }

    try {
      process.kill(-pid, 'SIGTERM');
    } catch {
      child.kill('SIGTERM');
    }
  }

  private async findLatestTrace(workspaceRoot: string): Promise<TraceSummary> {
    const traceDir = path.join(workspaceRoot, 'artifacts', 'runs');

    let names: string[];
    try {
      names = await fs.readdir(traceDir);
    } catch {
      return { tracePath: null, finalOutput: '' };
    }

    const candidates = await Promise.all(
      names
        .filter((name) => name.endsWith('.json'))
        .map(async (name) => {
          const fullPath = path.join(traceDir, name);
          const stats = await fs.stat(fullPath);
          return { fullPath, mtimeMs: stats.mtimeMs };
        }),
    );

    if (candidates.length === 0) {
      return { tracePath: null, finalOutput: '' };
    }

    candidates.sort((left, right) => right.mtimeMs - left.mtimeMs);
    const latest = candidates[0];

    try {
      const raw = await fs.readFile(latest.fullPath, 'utf8');
      const parsed: unknown = JSON.parse(raw);
      const finalOutput = isRecord(parsed) ? this.formatOutput(parsed.final) : '';
      return {
        tracePath: this.toDisplayPath(workspaceRoot, latest.fullPath),
        finalOutput,
      };
    } catch (error: unknown) {
      this.appendLog(`[trace] failed to parse ${latest.fullPath}: ${asErrorMessage(error)}`);
      return {
        tracePath: this.toDisplayPath(workspaceRoot, latest.fullPath),
        finalOutput: '',
      };
    }
  }

  private async resolveRequest(initialRequest?: string): Promise<string | undefined> {
    const trimmed = initialRequest?.trim();
    if (trimmed) {
      return trimmed;
    }

    const value = await vscode.window.showInputBox({
      prompt: 'Lula request',
      placeHolder: 'Summarize the latest trace and next steps',
      ignoreFocusOut: true,
    });

    const input = value?.trim();
    return input ? input : undefined;
  }

  private getWorkspaceRoot(): string | null {
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (workspaceRoot) {
      return workspaceRoot;
    }

    void vscode.window.showWarningMessage('Open a workspace folder to use Lula.');
    return null;
  }

  private formatOutput(value: unknown): string {
    if (typeof value === 'string') {
      return value;
    }
    if (value === undefined || value === null) {
      return '';
    }
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }

  private toDisplayPath(workspaceRoot: string, targetPath: string): string {
    const relativePath = path.relative(workspaceRoot, targetPath);
    if (relativePath !== '' && !relativePath.startsWith('..') && !path.isAbsolute(relativePath)) {
      return relativePath;
    }
    return targetPath;
  }

  private appendLog(line: string): void {
    this.logLines.push(`[${new Date().toISOString()}] ${line}`);
    if (this.logLines.length > LOG_LIMIT) {
      this.logLines.splice(0, this.logLines.length - LOG_LIMIT);
    }
    this.refresh();
  }

  private refresh(): void {
    if (!this.panel) {
      return;
    }

    void this.panel.webview.postMessage(this.getViewModel());
  }

  private getViewModel(): ViewModel {
    return {
      workspaceRoot: this.getWorkspaceRoot() ?? '(no workspace)',
      runnerStatus: this.runnerStatus,
      requestStatus: this.requestStatus,
      latestTracePath: this.latestTracePath ?? '(none)',
      finalOutput: this.latestFinalOutput || '(empty)',
      logs: this.logLines.length > 0 ? this.logLines.join('\n') : '(no logs yet)',
    };
  }

  private renderHtml(): string {
    const nonce = createNonce();

    return `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta
      http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';"
    />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Lula</title>
    <style>
      :root {
        color-scheme: light dark;
      }

      body {
        font-family: var(--vscode-font-family);
        margin: 0;
        padding: 16px;
      }

      .toolbar {
        display: flex;
        gap: 8px;
        margin-bottom: 16px;
      }

      .toolbar input {
        flex: 1;
        min-width: 180px;
      }

      button,
      input {
        color: var(--vscode-input-foreground);
        background: var(--vscode-input-background);
        border: 1px solid var(--vscode-input-border);
        padding: 6px 8px;
      }

      button {
        cursor: pointer;
      }

      section {
        margin-bottom: 16px;
      }

      h2 {
        font-size: 12px;
        letter-spacing: 0.08em;
        margin: 0 0 8px;
        text-transform: uppercase;
      }

      .meta {
        display: grid;
        gap: 4px;
      }

      pre {
        background: var(--vscode-textCodeBlock-background);
        border-radius: 6px;
        margin: 0;
        max-height: 320px;
        overflow: auto;
        padding: 12px;
        white-space: pre-wrap;
        word-break: break-word;
      }
    </style>
  </head>
  <body>
    <div class="toolbar">
      <input id="request" type="text" placeholder="Enter request" />
      <button id="run">Run Request</button>
      <button id="start">Start Runner</button>
      <button id="stop">Stop Runner</button>
    </div>

    <section>
      <h2>Status</h2>
      <div class="meta">
        <div id="workspace"></div>
        <div id="runner"></div>
        <div id="requestStatus"></div>
        <div id="tracePath"></div>
      </div>
    </section>

    <section>
      <h2>Final Output</h2>
      <pre id="finalOutput"></pre>
    </section>

    <section>
      <h2>Logs</h2>
      <pre id="logs"></pre>
    </section>

    <script nonce="${nonce}">
      const vscodeApi = acquireVsCodeApi();
      const requestInput = document.getElementById('request');
      const logs = document.getElementById('logs');

      document.getElementById('run').addEventListener('click', () => {
        vscodeApi.postMessage({ type: 'runRequest', request: requestInput.value });
      });
      document.getElementById('start').addEventListener('click', () => {
        vscodeApi.postMessage({ type: 'startRunner' });
      });
      document.getElementById('stop').addEventListener('click', () => {
        vscodeApi.postMessage({ type: 'stopRunner' });
      });
      requestInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          vscodeApi.postMessage({ type: 'runRequest', request: requestInput.value });
        }
      });

      window.addEventListener('message', (event) => {
        const state = event.data;
        document.getElementById('workspace').textContent = 'Workspace: ' + state.workspaceRoot;
        document.getElementById('runner').textContent = 'Runner: ' + state.runnerStatus;
        document.getElementById('requestStatus').textContent = 'Request: ' + state.requestStatus;
        document.getElementById('tracePath').textContent = 'Latest trace: ' + state.latestTracePath;
        document.getElementById('finalOutput').textContent = state.finalOutput;
        logs.textContent = state.logs;
        logs.scrollTop = logs.scrollHeight;
      });
    </script>
  </body>
</html>`;
  }
}

let extensionInstance: LgOrchExtension | undefined;

export function activate(context: vscode.ExtensionContext): void {
  extensionInstance = new LgOrchExtension(context);
  extensionInstance.register();
}

export async function deactivate(): Promise<void> {
  if (extensionInstance) {
    await extensionInstance.dispose();
    extensionInstance = undefined;
  }
}

function createNonce(): string {
  return `${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function asErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isRemoteRunInProgress(status: string): boolean {
  return status === 'starting' || status === 'queued' || status === 'running';
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}
