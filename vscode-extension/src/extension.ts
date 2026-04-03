import * as http from 'http';
import * as https from 'https';
import * as vscode from 'vscode';
import { OrchestratorClient, RunSummary } from './api/OrchestratorClient';
import { RunTreeProvider, RunItem } from './RunTreeProvider';
import { RunPanelProvider } from './RunPanelProvider';
import { StatusBarManager } from './providers/StatusBarManager';

const API_TOKEN_KEY = 'lula.apiToken';

async function getApiToken(context: vscode.ExtensionContext): Promise<string | undefined> {
    return context.secrets.get(API_TOKEN_KEY);
}

function getServerUrl(): string {
    const config = vscode.workspace.getConfiguration('lula');
    return config.get<string>('serverUrl', 'https://lula.eiv.eng.br');
}

async function submitTask(
    task: string,
    serverUrl: string,
    token: string,
): Promise<{ run_id: string } | null> {
    const url = new URL('/v1/runs', serverUrl);

    return new Promise((resolve, reject) => {
        const body = JSON.stringify({ request: task });
        const client = url.protocol === 'https:' ? https : http;
        const options: http.RequestOptions = {
            hostname: url.hostname,
            port: url.port || (url.protocol === 'https:' ? 443 : 80),
            path: url.pathname,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`,
                'Content-Length': Buffer.byteLength(body),
            },
        };

        const req = client.request(options, (res) => {
            let data = '';
            res.on('data', (chunk: Buffer | string) => {
                data += Buffer.isBuffer(chunk) ? chunk.toString('utf8') : chunk;
            });
            res.on('end', () => {
                try {
                    resolve(JSON.parse(data) as { run_id: string });
                } catch {
                    resolve(null);
                }
            });
        });
        req.on('error', reject);
        req.write(body);
        req.end();
    });
}

export function activate(context: vscode.ExtensionContext): void {
    const statusBar = new StatusBarManager();
    context.subscriptions.push({ dispose: () => statusBar.dispose() });

    let client: OrchestratorClient | null = null;
    let treeProvider: RunTreeProvider | null = null;

    // Status callback for RunPanelProvider
    let statusResetTimer: ReturnType<typeof setTimeout> | undefined;
    const onStatusUpdate = (run: RunSummary | null): void => {
        statusBar.update(run);
        if (statusResetTimer) {
            clearTimeout(statusResetTimer);
            statusResetTimer = undefined;
        }
        if (run && (run.status === 'succeeded' || run.status === 'failed' || run.status === 'cancelled')) {
            statusResetTimer = setTimeout(() => {
                statusBar.update(null);
            }, 30_000);
        }
    };

    const panelProvider = new RunPanelProvider(context.extensionUri, onStatusUpdate);
    context.subscriptions.push({ dispose: () => panelProvider.dispose() });

    const ensureClient = async (): Promise<OrchestratorClient | null> => {
        const token = await getApiToken(context);
        const url = getServerUrl();
        if (token) {
            client = new OrchestratorClient(url, token);
            return client;
        }
        return null;
    };

    // Always register tree view (even without token - it will just show empty)
    const initTree = async (): Promise<void> => {
        const c = await ensureClient();
        if (c && !treeProvider) {
            treeProvider = new RunTreeProvider(c);
            const treeView = vscode.window.createTreeView('orchestratorRuns', {
                treeDataProvider: treeProvider
            });
            context.subscriptions.push(treeView);
            context.subscriptions.push({ dispose: () => treeProvider?.dispose() });
        }
    };
    void initTree();

    // Command: Run Task
    context.subscriptions.push(
        vscode.commands.registerCommand('lula.runTask', async () => {
            const token = await getApiToken(context);
            if (!token) {
                const action = await vscode.window.showErrorMessage(
                    'Lula API token not configured.',
                    'Configure',
                );
                if (action === 'Configure') {
                    await vscode.commands.executeCommand('lula.configure');
                }
                return;
            }

            const task = await vscode.window.showInputBox({
                prompt: 'Describe the coding task for Lula',
                placeHolder: 'e.g. Write a Python function that sorts a list of dicts by key',
            });
            if (!task) {
                return;
            }

            // Capture active editor context (file path and selection)
            const editor = vscode.window.activeTextEditor;
            let editorContext = '';
            if (editor) {
                const selection = editor.selection;
                const selectedText = editor.document.getText(selection);
                const filePath = vscode.workspace.asRelativePath(editor.document.uri);
                if (selectedText) {
                    editorContext = `\n\nContext — ${filePath} (selected):\n\`\`\`\n${selectedText}\n\`\`\``;
                } else {
                    editorContext = `\n\nContext — active file: ${filePath}`;
                }
            }
            const fullTask = task + editorContext;

            const currentServerUrl = getServerUrl();

            await vscode.window.withProgress(
                {
                    location: vscode.ProgressLocation.Notification,
                    title: 'Lula: Submitting task...',
                    cancellable: false,
                },
                async () => {
                    try {
                        const result = await submitTask(fullTask, currentServerUrl, token);
                        if (result?.run_id) {
                            const shortId = result.run_id.slice(0, 8);
                            // Update status bar
                            onStatusUpdate({
                                run_id: result.run_id,
                                status: 'running',
                                request: fullTask,
                                started_at: new Date().toISOString(),
                                cancellable: true,
                                pending_approval: false
                            });

                            // Auto-open run panel — ensure client exists
                            const c = await ensureClient();
                            if (c) {
                                panelProvider.openPanel(result.run_id, c, context);
                            }

                            vscode.window.showInformationMessage(
                                `Lula task started (${shortId})`,
                            );
                            // Refresh tree view
                            treeProvider?.refresh();
                        } else {
                            await vscode.window.showErrorMessage('Lula: Failed to submit task');
                        }
                    } catch (e: unknown) {
                        const msg = e instanceof Error ? e.message : String(e);
                        await vscode.window.showErrorMessage(`Lula: ${msg}`);
                    }
                },
            );
        }),
    );

    // Command: Show Recent Runs
    context.subscriptions.push(
        vscode.commands.registerCommand('lula.showRuns', async () => {
            const url = getServerUrl();
            await vscode.env.openExternal(vscode.Uri.parse(url));
        }),
    );

    // Command: Open Run Panel (by runId)
    context.subscriptions.push(
        vscode.commands.registerCommand('lula.openRunPanel', async (runIdOrItem?: string | RunItem) => {
            let runId: string | undefined;
            if (typeof runIdOrItem === 'string') {
                runId = runIdOrItem;
            } else if (runIdOrItem instanceof RunItem) {
                runId = runIdOrItem.runId;
            }

            if (!runId) {
                // Prompt for run ID if not provided
                runId = await vscode.window.showInputBox({
                    prompt: 'Enter the Run ID to open',
                    placeHolder: 'run-id',
                });
            }
            if (!runId) {
                return;
            }

            const c = client || await ensureClient();
            if (!c) {
                vscode.window.showErrorMessage('Lula: No API token configured. Run "Lula: Configure" first.');
                return;
            }
            panelProvider.openPanel(runId, c, context);
        }),
    );

    // Command: Approve Run
    context.subscriptions.push(
        vscode.commands.registerCommand('lula.approveRun', async (runId?: string) => {
            if (!runId) {
                runId = await vscode.window.showInputBox({ prompt: 'Run ID to approve' });
            }
            if (!runId) return;
            const c = client || await ensureClient();
            if (!c) return;
            try {
                await c.approveRun(runId);
                vscode.window.showInformationMessage(`Lula: Run ${runId.slice(0, 8)} approved`);
                treeProvider?.refresh();
            } catch (e: unknown) {
                const msg = e instanceof Error ? e.message : String(e);
                vscode.window.showErrorMessage(`Lula: ${msg}`);
            }
        }),
    );

    // Command: Reject Run
    context.subscriptions.push(
        vscode.commands.registerCommand('lula.rejectRun', async (runId?: string) => {
            if (!runId) {
                runId = await vscode.window.showInputBox({ prompt: 'Run ID to reject' });
            }
            if (!runId) return;
            const c = client || await ensureClient();
            if (!c) return;
            try {
                await c.rejectRun(runId);
                vscode.window.showInformationMessage(`Lula: Run ${runId.slice(0, 8)} rejected`);
                treeProvider?.refresh();
            } catch (e: unknown) {
                const msg = e instanceof Error ? e.message : String(e);
                vscode.window.showErrorMessage(`Lula: ${msg}`);
            }
        }),
    );

    // Command: Cancel Run
    context.subscriptions.push(
        vscode.commands.registerCommand('lula.cancelRun', async (runId?: string) => {
            if (!runId) {
                runId = await vscode.window.showInputBox({ prompt: 'Run ID to cancel' });
            }
            if (!runId) return;
            const c = client || await ensureClient();
            if (!c) return;
            try {
                await c.cancelRun(runId);
                vscode.window.showInformationMessage(`Lula: Run ${runId.slice(0, 8)} cancelled`);
                treeProvider?.refresh();
            } catch (e: unknown) {
                const msg = e instanceof Error ? e.message : String(e);
                vscode.window.showErrorMessage(`Lula: ${msg}`);
            }
        }),
    );

    // Command: Configure
    context.subscriptions.push(
        vscode.commands.registerCommand('lula.configure', async () => {
            const token = await vscode.window.showInputBox({
                prompt: 'Enter your Lula API token',
                password: true,
                placeHolder: 'Bearer token from the Lula server',
            });
            if (token) {
                await context.secrets.store(API_TOKEN_KEY, token);
                await vscode.window.showInformationMessage('Lula: API token saved');
            }

            const currentUrl = getServerUrl();
            const newUrl = await vscode.window.showInputBox({
                prompt: 'Enter the Lula server URL',
                value: currentUrl,
            });
            if (newUrl) {
                await vscode.workspace.getConfiguration('lula').update(
                    'serverUrl',
                    newUrl,
                    vscode.ConfigurationTarget.Global,
                );
            }

            // Reinitialize client and tree view after config change
            const c = await ensureClient();
            if (c && !treeProvider) {
                treeProvider = new RunTreeProvider(c);
                const treeView = vscode.window.createTreeView('orchestratorRuns', {
                    treeDataProvider: treeProvider
                });
                context.subscriptions.push(treeView);
                context.subscriptions.push({ dispose: () => treeProvider?.dispose() });
            }
            treeProvider?.refresh();
        }),
    );

    // Sidebar tree view commands
    context.subscriptions.push(
        vscode.commands.registerCommand('orchestrator.refreshRuns', async () => {
            const c = await ensureClient();
            if (c && !treeProvider) {
                treeProvider = new RunTreeProvider(c);
                const treeView = vscode.window.createTreeView('orchestratorRuns', {
                    treeDataProvider: treeProvider
                });
                context.subscriptions.push(treeView);
                context.subscriptions.push({ dispose: () => treeProvider?.dispose() });
            }
            treeProvider?.refresh();
        }),
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('orchestrator.openRun', (item: RunItem) => {
            if (client) {
                panelProvider.openPanel(item.runId, client, context);
            }
        }),
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('orchestrator.newRun', async () => {
            await vscode.commands.executeCommand('lula.runTask');
        }),
    );
}

export function deactivate(): void {
    // no-op
}
