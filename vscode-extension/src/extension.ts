import { randomUUID } from 'crypto';
import { spawn, type ChildProcess } from 'child_process';
import * as http from 'http';
import * as https from 'https';
import * as fs from 'fs/promises';
import * as path from 'path';
import * as vscode from 'vscode';
import { OrchestratorClient } from './api/OrchestratorClient';
import { RunTreeProvider, RunItem } from './RunTreeProvider';
import { RunPanelProvider } from './RunPanelProvider';

const DEFAULT_REMOTE_POLL_INTERVAL_MS = 1000;
const LOG_LIMIT = 1000;

const COMMANDS = {
  openPanel: 'lgOrch.openPanel',
  runRequest: 'lgOrch.runRequest',
  startRunner: 'lgOrch.startRunner',
  stopRunner: 'lgOrch.stopRunner',
  openRunHistory: 'lgOrch.openRunHistory',
  clearRunHistory: 'lgOrch.clearRunHistory',
} as const;

interface RunHistoryEntry {
  runId: string;
  request: string;
  status: string;
  startedAt: string;
  tracePath: string | null;
  finalOutput: string;
}

interface TraceSummary {
  tracePath: string | null;
  finalOutput: string;
}

interface ViewModel {
  workspaceRoot: string;
  runnerStatus: string;
  requestStatus: string;
  stopLabel: string;
  latestTracePath: string;
  finalOutput: string;
  logs: string;
  runHistory: RunHistoryEntry[];
  showInlineDiff: boolean;
  inlineDiff: string;
  verifierReport: string;
  pendingApproval: boolean;
  pendingApprovalSummary: string;
  approvalHistory: string;
}

interface RemoteRunDetails {
  run_id?: unknown;
  status?: unknown;
  exit_code?: unknown;
  cancel_requested?: unknown;
  cancellable?: unknown;
  trace_path?: unknown;
  trace_ready?: unknown;
  trace?: unknown;
  pending_approval?: unknown;
  pending_approval_summary?: unknown;
  approval_history?: unknown;
  checkpoint_id?: unknown;
  thread_id?: unknown;
  verification?: unknown;
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
  private latestInlineDiff = '';
  private latestVerifierReport = '';
  private runnerStatus = 'stopped';
  private requestStatus = 'idle';
  private requestRunning = false;
  private activeRemoteRunId: string | null = null;
  private pendingApproval = false;
  private pendingApprovalSummary = '';
  private approvalHistory = '';
  private readonly runHistory: RunHistoryEntry[] = [];

  private chatParticipant: vscode.ChatParticipant | undefined;

  public constructor(private readonly context: vscode.ExtensionContext) { }

  public register(): void {
    const diffProvider = new (class implements vscode.TextDocumentContentProvider {
      provideTextDocumentContent(uri: vscode.Uri): string {
        const params = new URLSearchParams(uri.query);
        const traceContent = params.get('diffContent');
        return traceContent ? decodeURIComponent(traceContent) : 'No diff available.';
      }
    })();
    this.context.subscriptions.push(
      vscode.workspace.registerTextDocumentContentProvider('lula-diff', diffProvider)
    );

    this.chatParticipant = vscode.chat.createChatParticipant('lula', this.handleChatRequest.bind(this));
    this.chatParticipant.iconPath = new vscode.ThemeIcon('hubot');
    this.context.subscriptions.push(this.chatParticipant);

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
      vscode.commands.registerCommand(COMMANDS.openRunHistory, async () => {
        await this.openPanel();
      }),
      vscode.commands.registerCommand(COMMANDS.clearRunHistory, () => {
        this.runHistory.splice(0, this.runHistory.length);
        this.refresh();
      }),
      vscode.commands.registerCommand('lgOrch.viewInlineDiff', (uri: vscode.Uri) => {
        vscode.commands.executeCommand('vscode.open', uri);
      }),
    );
  }

  public async dispose(): Promise<void> {
    await this.stopRunner(false);
    this.panel?.dispose();
    this.panel = undefined;
  }

  private async handleChatRequest(
    request: vscode.ChatRequest,
    context: vscode.ChatContext,
    response: vscode.ChatResponseStream,
    token: vscode.CancellationToken
  ): Promise<vscode.ChatResult | void> {
    if (request.command === 'run' || !request.command) {
      response.progress('Starting Lula task...');
      
      const success = await this.runRequestCore(request.prompt, (msg) => {
        response.progress(msg);
      }, token);

      if (success && this.latestFinalOutput) {
        response.markdown(`\n\n**Run Complete**\n\n\`\`\`\n${this.latestFinalOutput}\n\`\`\``);
        if (this.latestInlineDiff) {
          const encodedDiff = encodeURIComponent(this.latestInlineDiff);
          const uri = vscode.Uri.parse(`lula-diff:diff.patch?diffContent=${encodedDiff}`);
          response.markdown(`\n\n[View Patch](command:lgOrch.viewInlineDiff?${encodeURIComponent(JSON.stringify([uri]))})`);
        }
      } else if (token.isCancellationRequested) {
        response.markdown(`\n\n*Run Cancelled*`);
      } else {
        response.markdown(`\n\n*Run Failed or produced no output.* Check the Lula webview for detailed logs.`);
      }
      
      return { metadata: { command: request.command } };
    }
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
      case 'clearRunHistory':
        this.runHistory.splice(0, this.runHistory.length);
        this.refresh();
        return;
      case 'approveRun':
        await this.approveRemoteRun();
        return;
      case 'rejectRun':
        await this.rejectRemoteRun();
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

    const binaryPath = this.getRunnerBinaryPath();
    let command: string;
    let args: string[];
    let cwd: string;

    if (binaryPath) {
      command = binaryPath;
      args = [
        '--bind', this.getRunnerBindAddress(),
        '--root-dir', workspaceRoot,
        '--profile', 'dev',
        '--api-key', this.getRunnerApiKey(),
      ];
      cwd = workspaceRoot;
    } else {
      command = 'cargo';
      args = [
        'run', '--',
        '--bind', this.getRunnerBindAddress(),
        '--root-dir', workspaceRoot,
        '--profile', 'dev',
        '--api-key', this.getRunnerApiKey(),
      ];
      cwd = path.join(workspaceRoot, 'rs');
    }

    this.runnerStatus = 'starting';
    this.appendLog(`[runner] starting: ${command} ${args.join(' ')}`);

    const child = spawn(command, args, {
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

    if (this.requestRunning && this.activeRemoteRunId) {
      const remoteApiBaseUrl = this.getRemoteApiBaseUrl();
      if (remoteApiBaseUrl) {
        await this.cancelRemoteRun(remoteApiBaseUrl, this.activeRemoteRunId);
        return;
      }
    }

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
    await this.runRequestCore(initialRequest);
  }

  private async runRequestCore(
    initialRequest?: string,
    progressCallback?: (msg: string) => void,
    token?: vscode.CancellationToken
  ): Promise<boolean> {
    if (this.requestRunning) {
      this.appendLog('[run] request already in progress');
      return false;
    }

    const workspaceRoot = this.getWorkspaceRoot();
    if (!workspaceRoot) {
      return false;
    }

    const request = await this.resolveRequest(initialRequest);
    if (!request) {
      this.appendLog('[run] canceled');
      return false;
    }

    if (token?.isCancellationRequested) {
      return false;
    }

    this.requestRunning = true;
    this.requestStatus = 'starting';
    this.activeRemoteRunId = null;
    this.latestTracePath = null;
    this.latestFinalOutput = '';
    this.latestInlineDiff = '';
    this.latestVerifierReport = '';
    this.pendingApproval = false;
    this.pendingApprovalSummary = '';
    this.approvalHistory = '';
    this.appendLog(`[run] request: ${request}`);
    progressCallback?.('Initializing run...');
    this.refresh();

    let success = false;
    try {
      const remoteApiBaseUrl = this.getRemoteApiBaseUrl();
      if (remoteApiBaseUrl) {
        success = await this.runRemoteRequest(request, remoteApiBaseUrl, progressCallback, token);
      } else {
        success = await this.runLocalRequest(request, workspaceRoot, progressCallback, token);
      }
    } catch (error: unknown) {
      this.requestStatus = 'failed';
      this.appendLog(`[run] failed: ${asErrorMessage(error)}`);
    } finally {
      this.requestRunning = false;
      const maxHistory = this.getMaxRunHistory();
      this.runHistory.push({
        runId: this.activeRemoteRunId ?? `local-${Date.now()}`,
        request: request || '',
        status: this.requestStatus,
        startedAt: new Date().toISOString(),
        tracePath: this.latestTracePath,
        finalOutput: this.latestFinalOutput,
      });
      if (this.runHistory.length > maxHistory) {
        this.runHistory.splice(0, this.runHistory.length - maxHistory);
      }
      if (!this.pendingApproval) {
        this.activeRemoteRunId = null;
      }
      this.refresh();
    }
    return success;
  }

  private async runLocalRequest(
    request: string,
    workspaceRoot: string,
    progressCallback?: (msg: string) => void,
    token?: vscode.CancellationToken
  ): Promise<boolean> {
    const pyDir = path.join(workspaceRoot, 'py');
    this.requestStatus = 'running';
    progressCallback?.('Running local request...');

    const syncCode = await this.runCommand('uv-sync', 'uv', ['sync'], pyDir);
    if (syncCode !== 0 || token?.isCancellationRequested) {
      this.requestStatus = 'failed';
      this.appendLog('[run] uv sync failed or cancelled');
      return false;
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
      this.getRunnerBaseUrl(),
    ];
    
    // Check cancellation before long run
    if (token?.isCancellationRequested) return false;

    const runCode = await this.runCommand('cli', 'uv', runArgs, pyDir);
    const summary = await this.findLatestTrace(workspaceRoot);
    this.latestTracePath = summary.tracePath;
    this.latestFinalOutput = summary.finalOutput;
    this.latestVerifierReport = summary.verifierReport;

    if (summary.tracePath) {
      this.appendLog(`[trace] latest: ${summary.tracePath}`);
    } else {
      this.appendLog('[trace] no trace found');
    }

    if (runCode !== 0) {
      this.requestStatus = 'failed';
      this.appendLog('[run] command failed');
      return false;
    }

    this.requestStatus = 'succeeded';
    progressCallback?.('Run succeeded.');
    return true;
  }

  private async runRemoteRequest(
    request: string,
    remoteApiBaseUrl: string,
    progressCallback?: (msg: string) => void,
    token?: vscode.CancellationToken
  ): Promise<boolean> {
    const pollIntervalMs = this.getRemotePollIntervalMs();
    const remoteApiBearerToken = this.getRemoteApiBearerToken();
    this.appendLog(`[remote] using API: ${remoteApiBaseUrl}`);
    await this.requestJson('GET', `${remoteApiBaseUrl}/healthz`, undefined, remoteApiBearerToken);
    this.appendLog('[remote] healthz ok');

    const created = await this.requestJson<RemoteRunDetails>(
      'POST',
      `${remoteApiBaseUrl}/v1/runs`,
      {
        request,
        view: 'classic',
      },
      remoteApiBearerToken,
    );
    const runId = this.readRemoteRunId(created);
    this.activeRemoteRunId = runId;
    this.appendLog(`[remote] run started: ${runId}`);
    progressCallback?.(`Run started on remote server: ${runId}`);
    this.applyRemoteRunDetails(created);

    let logCount = 0;
    while (true) {
      if (token?.isCancellationRequested) {
        await this.cancelRemoteRun(remoteApiBaseUrl, runId);
        break;
      }

      const detail = await this.requestJson<RemoteRunDetails>(
        'GET',
        `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}`,
        undefined,
        remoteApiBearerToken,
      );
      this.applyRemoteRunDetails(detail);
      
      if (detail.status && typeof detail.status === 'string') {
        progressCallback?.(`Status: ${detail.status}`);
      }

      const logs = await this.requestJson<RemoteRunLogs>(
        'GET',
        `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}/logs`,
        undefined,
        remoteApiBearerToken,
      );
      logCount = this.appendRemoteLogs(logs, logCount);

      if (!isRemoteRunInProgress(this.requestStatus)) {
        break;
      }

      await delay(pollIntervalMs);
    }

    const finalDetail = await this.requestJson<RemoteRunDetails>(
      'GET',
      `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}`,
      undefined,
      remoteApiBearerToken,
    );
    this.applyRemoteRunDetails(finalDetail);
    this.appendRemoteLogs(
      await this.requestJson<RemoteRunLogs>(
        'GET',
        `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}/logs`,
        undefined,
        remoteApiBearerToken,
      ),
      logCount,
    );

    if (typeof finalDetail.exit_code === 'number') {
      this.appendLog(`[remote] completed exit_code=${finalDetail.exit_code}`);
    }

    if (token?.isCancellationRequested) {
      return false;
    }

    return finalDetail.status === 'succeeded' || finalDetail.exit_code === 0 || !!finalDetail.trace;
  }

  private async cancelRemoteRun(remoteApiBaseUrl: string, runId: string): Promise<void> {
    const remoteApiBearerToken = this.getRemoteApiBearerToken();
    this.requestStatus = 'cancelling';
    this.refresh();
    this.appendLog(`[remote] cancel requested: ${runId}`);
    const detail = await this.requestJson<RemoteRunDetails>(
      'POST',
      `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}/cancel`,
      {},
      remoteApiBearerToken,
    );
    this.applyRemoteRunDetails(detail);
  }

  private async approveRemoteRun(): Promise<void> {
    const remoteApiBaseUrl = this.getRemoteApiBaseUrl();
    const runId = this.activeRemoteRunId;
    if (!remoteApiBaseUrl || !runId) {
      this.appendLog('[approval] remote approval unavailable');
      return;
    }
    const remoteApiBearerToken = this.getRemoteApiBearerToken();
    this.appendLog(`[approval] approving ${runId}`);
    const detail = await this.requestJson<RemoteRunDetails>(
      'POST',
      `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}/approve`,
      { actor: 'vscode' },
      remoteApiBearerToken,
    );
    this.applyRemoteRunDetails(detail);
    if (runId && isRemoteRunInProgress(this.requestStatus)) {
      void this.followRemoteRunAfterApproval(remoteApiBaseUrl, runId);
    }
  }

  private async rejectRemoteRun(): Promise<void> {
    const remoteApiBaseUrl = this.getRemoteApiBaseUrl();
    const runId = this.activeRemoteRunId;
    if (!remoteApiBaseUrl || !runId) {
      this.appendLog('[approval] remote rejection unavailable');
      return;
    }
    const remoteApiBearerToken = this.getRemoteApiBearerToken();
    this.appendLog(`[approval] rejecting ${runId}`);
    const detail = await this.requestJson<RemoteRunDetails>(
      'POST',
      `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}/reject`,
      { actor: 'vscode' },
      remoteApiBearerToken,
    );
    this.applyRemoteRunDetails(detail);
  }

  private async followRemoteRunAfterApproval(remoteApiBaseUrl: string, runId: string): Promise<void> {
    const remoteApiBearerToken = this.getRemoteApiBearerToken();
    const pollIntervalMs = this.getRemotePollIntervalMs();
    let logCount = 0;
    this.requestRunning = true;
    this.activeRemoteRunId = runId;
    this.refresh();
    try {
      while (true) {
        const detail = await this.requestJson<RemoteRunDetails>(
          'GET',
          `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}`,
          undefined,
          remoteApiBearerToken,
        );
        this.applyRemoteRunDetails(detail);
        const logs = await this.requestJson<RemoteRunLogs>(
          'GET',
          `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}/logs`,
          undefined,
          remoteApiBearerToken,
        );
        logCount = this.appendRemoteLogs(logs, logCount);
        if (!isRemoteRunInProgress(this.requestStatus)) {
          break;
        }
        await delay(pollIntervalMs);
      }

      const finalDetail = await this.requestJson<RemoteRunDetails>(
        'GET',
        `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}`,
        undefined,
        remoteApiBearerToken,
      );
      this.applyRemoteRunDetails(finalDetail);
      this.appendRemoteLogs(
        await this.requestJson<RemoteRunLogs>(
          'GET',
          `${remoteApiBaseUrl}/v1/runs/${encodeURIComponent(runId)}/logs`,
          undefined,
          remoteApiBearerToken,
        ),
        logCount,
      );
    } catch (error: unknown) {
      this.appendLog(`[approval] follow-up failed: ${asErrorMessage(error)}`);
    } finally {
      this.requestRunning = false;
      this.refresh();
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

  private getRunnerBindAddress(): string {
    return vscode.workspace.getConfiguration('lula').get<string>('runnerBindAddress', '127.0.0.1:8088').trim() || '127.0.0.1:8088';
  }

  private getRunnerBaseUrl(): string {
    const bind = this.getRunnerBindAddress();
    return `http://${bind}`;
  }

  private getRunnerApiKey(): string {
    return vscode.workspace.getConfiguration('lula').get<string>('runnerApiKey', 'dev-insecure').trim() || 'dev-insecure';
  }

  private getRunnerBinaryPath(): string {
    return vscode.workspace.getConfiguration('lula').get<string>('runnerBinaryPath', '').trim();
  }

  private isShowInlineDiff(): boolean {
    return vscode.workspace.getConfiguration('lula').get<boolean>('showInlineDiff', true);
  }

  private getMaxRunHistory(): number {
    const val = vscode.workspace.getConfiguration('lula').get<number>('maxRunHistory', 20);
    if (!Number.isFinite(val) || val < 1) return 20;
    return Math.min(val, 200);
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

  private getRemoteApiBearerToken(): string | null {
    const raw = vscode.workspace.getConfiguration('lula').get<string>('remoteApiBearerToken', '').trim();
    return raw || null;
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
    const hasPendingApproval = detail.pending_approval === true;

    if (typeof detail.run_id === 'string' && detail.run_id.trim()) {
      this.activeRemoteRunId = detail.run_id.trim();
    }

    if (detail.cancellable === false || this.requestStatus === 'cancelled' || (!isRemoteRunInProgress(this.requestStatus) && !hasPendingApproval)) {
      this.activeRemoteRunId = null;
    }

    if (typeof detail.trace_path === 'string' && detail.trace_path.trim()) {
      this.latestTracePath = detail.trace_path;
    }

    if (detail.trace_ready === true && isRecord(detail.trace)) {
      this.latestFinalOutput = this.formatOutput(detail.trace.final);
      if (this.isShowInlineDiff()) {
        this.latestInlineDiff = this.extractInlineDiff(detail.trace);
      }
      if (detail.trace.verification !== undefined && detail.trace.verification !== null) {
        try {
          this.latestVerifierReport = JSON.stringify(detail.trace.verification, null, 2);
        } catch {
          this.latestVerifierReport = String(detail.trace.verification);
        }
      }
    }

    const approvalHistory = Array.isArray(detail.approval_history) ? detail.approval_history : [];
    if (approvalHistory.length > 0) {
      try {
        this.approvalHistory = JSON.stringify(approvalHistory, null, 2);
      } catch {
        this.approvalHistory = String(approvalHistory);
      }
    } else {
      this.approvalHistory = '';
    }

    if (hasPendingApproval) {
      this.pendingApproval = true;
      this.pendingApprovalSummary = typeof detail.pending_approval_summary === 'string'
        ? detail.pending_approval_summary : '';
    }

    if (!hasPendingApproval && !isRemoteRunInProgress(this.requestStatus)) {
      this.pendingApproval = false;
      this.pendingApprovalSummary = '';
    }

    this.refresh();
  }

  private extractInlineDiff(traceData: unknown): string {
    if (!isRecord(traceData)) return '';
    const toolResults = Array.isArray(traceData.tool_results) ? traceData.tool_results : [];
    const patches: string[] = [];
    for (const result of toolResults) {
      if (!isRecord(result)) continue;
      if (String(result.tool || '').includes('apply_patch') && Boolean(result.ok)) {
        const input = isRecord(result.input) ? result.input : {};
        const patch = typeof input.patch === 'string' ? input.patch.trim() : '';
        const changes = Array.isArray(input.changes) ? input.changes : [];
        if (patch) {
          patches.push(patch);
        } else {
          for (const change of changes) {
            if (isRecord(change)) {
              const content = typeof change.patch === 'string' ? change.patch.trim()
                : typeof change.content === 'string' ? change.content.trim() : '';
              const filePath = typeof change.path === 'string' ? change.path : '(unknown)';
              if (content) {
                patches.push(`--- ${filePath}\n${content}`);
              }
            }
          }
        }
      }
    }
    return patches.join('\n\n---\n\n');
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

  private async requestJson<T>(method: 'GET' | 'POST', requestUrl: string, body?: unknown, bearerToken?: string | null): Promise<T> {
    const url = new URL(requestUrl);
    const client = url.protocol === 'https:' ? https : url.protocol === 'http:' ? http : null;
    if (!client) {
      throw new Error(`unsupported protocol: ${url.protocol}`);
    }

    const payload = body === undefined ? undefined : JSON.stringify(body);
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'X-Request-ID': randomUUID(),
    };
    if (bearerToken && bearerToken.trim()) {
      headers.Authorization = `Bearer ${bearerToken.trim()}`;
    }
    if (payload !== undefined) {
      headers['Content-Type'] = 'application/json';
      headers['Content-Length'] = Buffer.byteLength(payload).toString();
    }

    return await new Promise<T>((resolve, reject) => {
      const request = client.request(
        {
          hostname: url.hostname,
          port: url.port,
          path: `${url.pathname}${url.search}`,
          method,
          headers,
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

  private async findLatestTrace(workspaceRoot: string): Promise<TraceSummary & { verifierReport: string }> {
    const traceDir = path.join(workspaceRoot, 'artifacts', 'runs');

    let names: string[];
    try {
      names = await fs.readdir(traceDir);
    } catch {
      return { tracePath: null, finalOutput: '', verifierReport: '' };
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
      return { tracePath: null, finalOutput: '', verifierReport: '' };
    }

    candidates.sort((left, right) => right.mtimeMs - left.mtimeMs);
    const latest = candidates[0];

    try {
      const raw = await fs.readFile(latest.fullPath, 'utf8');
      const parsed: unknown = JSON.parse(raw);
      const finalOutput = isRecord(parsed) ? this.formatOutput(parsed.final) : '';
      let verifierReport = '';
      if (isRecord(parsed) && parsed.verification !== undefined && parsed.verification !== null) {
        try {
          verifierReport = JSON.stringify(parsed.verification, null, 2);
        } catch {
          verifierReport = String(parsed.verification);
        }
      }
      return {
        tracePath: this.toDisplayPath(workspaceRoot, latest.fullPath),
        finalOutput,
        verifierReport,
      };
    } catch (error: unknown) {
      this.appendLog(`[trace] failed to parse ${latest.fullPath}: ${asErrorMessage(error)}`);
      return {
        tracePath: this.toDisplayPath(workspaceRoot, latest.fullPath),
        finalOutput: '',
        verifierReport: '',
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
      stopLabel: this.requestRunning && this.activeRemoteRunId ? 'Cancel Remote Run' : 'Stop Runner',
      latestTracePath: this.latestTracePath ?? '(none)',
      finalOutput: this.latestFinalOutput || '(empty)',
      logs: this.logLines.length > 0 ? this.logLines.join('\n') : '(no logs yet)',
      runHistory: [...this.runHistory].reverse(),
      showInlineDiff: this.isShowInlineDiff(),
      inlineDiff: this.latestInlineDiff,
      verifierReport: this.latestVerifierReport,
      pendingApproval: this.pendingApproval,
      pendingApprovalSummary: this.pendingApprovalSummary,
      approvalHistory: this.approvalHistory,
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

      .run-history { display: flex; flex-direction: column; gap: 4px; }
      .run-entry { display: grid; grid-template-columns: auto 1fr auto; gap: 8px; align-items: center; padding: 4px 8px; background: var(--vscode-textCodeBlock-background); border-radius: 4px; font-size: 11px; }
      .run-entry .status-ok { color: var(--vscode-testing-iconPassed); }
      .run-entry .status-fail { color: var(--vscode-testing-iconFailed); }
      .run-entry .status-other { color: var(--vscode-descriptionForeground); }
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

    <section id="approvalSection" style="display:none">
      <h2>&#x26A0; Pending Approval</h2>
      <div id="approvalSummary" style="padding:8px;background:var(--vscode-inputValidation-warningBackground);border-radius:4px;"></div>
      <div style="margin-top:8px;display:flex;gap:8px;">
        <button id="approveBtn">Approve</button>
        <button id="rejectBtn">Reject</button>
      </div>
    </section>

    <section id="approvalHistorySection" style="display:none">
      <h2>Approval History</h2>
      <pre id="approvalHistory"></pre>
    </section>

    <section>
      <h2>Final Output</h2>
      <pre id="finalOutput"></pre>
    </section>

    <section id="verifierSection">
      <h2>Verifier Report</h2>
      <pre id="verifierReport"></pre>
    </section>

    <section id="diffSection" style="display:none">
      <h2>Inline Diff</h2>
      <pre id="inlineDiff"></pre>
    </section>

    <section>
      <h2>Run History <button id="clearHistory" style="font-size:10px;padding:2px 6px">Clear</button></h2>
      <div id="runHistory" class="run-history"></div>
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

      function escapeHtml(text) {
        return String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
      }

      window.addEventListener('message', (event) => {
        const state = event.data;
        document.getElementById('workspace').textContent = 'Workspace: ' + state.workspaceRoot;
        document.getElementById('runner').textContent = 'Runner: ' + state.runnerStatus;
        document.getElementById('requestStatus').textContent = 'Request: ' + state.requestStatus;
        document.getElementById('stop').textContent = state.stopLabel;
        document.getElementById('tracePath').textContent = 'Latest trace: ' + state.latestTracePath;
        document.getElementById('finalOutput').textContent = state.finalOutput;
        logs.textContent = state.logs;
        logs.scrollTop = logs.scrollHeight;

        const diffSection = document.getElementById('diffSection');
        const inlineDiff = document.getElementById('inlineDiff');
        if (state.showInlineDiff && state.inlineDiff) {
          inlineDiff.textContent = state.inlineDiff;
          diffSection.style.display = '';
        } else {
          diffSection.style.display = 'none';
        }

        const verifierReportEl = document.getElementById('verifierReport');
        const verifierSection = document.getElementById('verifierSection');
        if (state.verifierReport) {
          verifierReportEl.textContent = state.verifierReport;
          verifierSection.style.display = '';
        } else {
          verifierReportEl.textContent = '';
          verifierSection.style.display = 'none';
        }

        const approvalSection = document.getElementById('approvalSection');
        const approvalSummary = document.getElementById('approvalSummary');
        if (state.pendingApproval) {
          approvalSummary.textContent = state.pendingApprovalSummary || 'Mutation plan awaiting approval.';
          approvalSection.style.display = '';
        } else {
          approvalSection.style.display = 'none';
        }

        const approvalHistorySection = document.getElementById('approvalHistorySection');
        const approvalHistory = document.getElementById('approvalHistory');
        if (state.approvalHistory) {
          approvalHistory.textContent = state.approvalHistory;
          approvalHistorySection.style.display = '';
        } else {
          approvalHistory.textContent = '';
          approvalHistorySection.style.display = 'none';
        }

        const historyContainer = document.getElementById('runHistory');
        historyContainer.innerHTML = '';
        for (const entry of (state.runHistory || [])) {
          const div = document.createElement('div');
          div.className = 'run-entry';
          const statusClass = entry.status === 'succeeded' ? 'status-ok'
            : entry.status === 'failed' ? 'status-fail' : 'status-other';
          div.innerHTML = '<span class="' + statusClass + '">' + escapeHtml(entry.status) + '</span><span title="' + escapeHtml(entry.request) + '">' + escapeHtml(entry.request.length > 60 ? entry.request.slice(0, 60) + '...' : entry.request) + '</span><span>' + escapeHtml(entry.startedAt.slice(11, 19)) + '</span>';
          historyContainer.appendChild(div);
        }
      });

      document.getElementById('clearHistory').addEventListener('click', () => {
        vscodeApi.postMessage({ type: 'clearRunHistory' });
      });

      document.getElementById('approveBtn').addEventListener('click', () => {
        vscodeApi.postMessage({ type: 'approveRun' });
      });
      document.getElementById('rejectBtn').addEventListener('click', () => {
        vscodeApi.postMessage({ type: 'rejectRun' });
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

  const getClient = (): OrchestratorClient => {
    const baseUrl = vscode.workspace
      .getConfiguration('lula')
      .get<string>('remoteApiBaseUrl', '')
      .trim() || 'http://localhost:8765';
    const token = vscode.workspace
      .getConfiguration('lula')
      .get<string>('remoteApiBearerToken', '')
      .trim() || null;
    return new OrchestratorClient(baseUrl, token);
  };

  const runTreeProvider = new RunTreeProvider(getClient());
  const runPanelProvider = new RunPanelProvider();

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider('orchestratorRuns', runTreeProvider),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('orchestrator.refreshRuns', () => {
      runTreeProvider.refresh();
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('orchestrator.openRun', (item: RunItem) => {
      runPanelProvider.openPanel(item.runId, getClient(), context);
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('orchestrator.newRun', async () => {
      const task = await vscode.window.showInputBox({ prompt: 'Task description', ignoreFocusOut: true });
      if (!task || !task.trim()) {
        return;
      }
      const client = getClient();
      try {
        const run = await client.postRun(task.trim());
        runTreeProvider.refresh();
        void vscode.window.showInformationMessage(`Run started: ${run.run_id}`);
      } catch (error: unknown) {
        void vscode.window.showErrorMessage(
          `Failed to start run: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    }),
  );
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
