import * as http from 'http';
import * as https from 'https';
import * as vscode from 'vscode';
import { OrchestratorClient } from './api/OrchestratorClient';
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
        const body = JSON.stringify({ task });
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
    const serverUrl = getServerUrl();
    const statusBar = new StatusBarManager();
    context.subscriptions.push({ dispose: () => statusBar.dispose() });

    // Tree view for sidebar (uses OrchestratorClient for listing runs)
    let client: OrchestratorClient | null = null;
    let treeProvider: RunTreeProvider | null = null;
    const panelProvider = new RunPanelProvider();

    const ensureClient = async (): Promise<OrchestratorClient | null> => {
        const token = await getApiToken(context);
        const url = getServerUrl();
        if (token) {
            client = new OrchestratorClient(url, token);
            return client;
        }
        return null;
    };

    // Register tree view if client is available
    void ensureClient().then((c) => {
        if (c) {
            treeProvider = new RunTreeProvider(c);
            vscode.window.registerTreeDataProvider('orchestratorRuns', treeProvider);
        }
    });

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

            const currentServerUrl = getServerUrl();

            await vscode.window.withProgress(
                {
                    location: vscode.ProgressLocation.Notification,
                    title: 'Lula: Submitting task...',
                    cancellable: false,
                },
                async () => {
                    try {
                        const result = await submitTask(task, currentServerUrl, token);
                        if (result?.run_id) {
                            const shortId = result.run_id.slice(0, 8);
                            const action = await vscode.window.showInformationMessage(
                                `Lula task started (${shortId})`,
                                'Open Console',
                            );
                            if (action === 'Open Console') {
                                await vscode.env.openExternal(
                                    vscode.Uri.parse(`${currentServerUrl}?run=${result.run_id}`),
                                );
                            }
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

    // Command: Open Console
    context.subscriptions.push(
        vscode.commands.registerCommand('lula.openConsole', async () => {
            const url = getServerUrl();
            await vscode.env.openExternal(vscode.Uri.parse(url));
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
                vscode.window.registerTreeDataProvider('orchestratorRuns', treeProvider);
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
                vscode.window.registerTreeDataProvider('orchestratorRuns', treeProvider);
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
