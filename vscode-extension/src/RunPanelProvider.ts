import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { OrchestratorClient, RunSummary } from './api/OrchestratorClient';

function getNonce(): string {
    let text = '';
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    for (let i = 0; i < 32; i++) {
        text += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return text;
}

export class RunPanelProvider {
    private readonly panels: Map<string, vscode.WebviewPanel> = new Map();
    private readonly cancellers: Map<string, () => void> = new Map();

    constructor(
        private readonly extensionUri: vscode.Uri,
        private readonly onStatusUpdate: (run: RunSummary | null) => void
    ) {}

    public openPanel(runId: string, client: OrchestratorClient, context: vscode.ExtensionContext): void {
        // Reuse existing panel
        const existing = this.panels.get(runId);
        if (existing) {
            existing.reveal(vscode.ViewColumn.Beside);
            return;
        }

        const panel = vscode.window.createWebviewPanel(
            'lulaRun',
            `Lula: ${runId.slice(0, 8)}`,
            vscode.ViewColumn.Beside,
            {
                enableScripts: true,
                localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, 'out', 'webview')],
                retainContextWhenHidden: true
            }
        );

        this.panels.set(runId, panel);
        panel.webview.html = this.getHtml(panel.webview);

        // Fetch initial state
        client.getRunDetail(runId).then(detail => {
            panel.webview.postMessage({ type: 'run-state', data: detail });
            this.onStatusUpdate(detail as any);
        }).catch(() => {});

        // Start SSE stream
        const cancelStream = client.streamRun(
            runId,
            (event) => {
                panel.webview.postMessage({ type: 'sse-event', data: event });

                // Check for approval
                if (event.type === 'approval_requested') {
                    panel.webview.postMessage({
                        type: 'approval-requested',
                        data: event
                    });
                }
            },
            () => {
                panel.webview.postMessage({ type: 'run-done' });
                this.onStatusUpdate(null);
            }
        );
        this.cancellers.set(runId, cancelStream);

        // Handle messages from webview
        panel.webview.onDidReceiveMessage(async msg => {
            switch (msg.type) {
                case 'approve':
                    await client.approveRun(runId);
                    break;
                case 'reject':
                    await client.rejectRun(runId);
                    break;
                case 'cancel':
                    await client.cancelRun(runId);
                    break;
                case 'open-file':
                    if (msg.path) {
                        const doc = await vscode.workspace.openTextDocument(msg.path);
                        vscode.window.showTextDocument(doc);
                    }
                    break;
            }
        });

        panel.onDidDispose(() => {
            this.panels.delete(runId);
            const cancel = this.cancellers.get(runId);
            if (cancel) {
                cancel();
                this.cancellers.delete(runId);
            }
        });
    }

    public dispose(): void {
        for (const cancel of this.cancellers.values()) cancel();
        for (const panel of this.panels.values()) panel.dispose();
        this.panels.clear();
        this.cancellers.clear();
    }

    private getHtml(webview: vscode.Webview): string {
        const webviewDir = vscode.Uri.joinPath(this.extensionUri, 'out', 'webview');
        const cssUri = webview.asWebviewUri(vscode.Uri.joinPath(webviewDir, 'run-panel.css'));
        const jsUri = webview.asWebviewUri(vscode.Uri.joinPath(webviewDir, 'run-panel.js'));
        const nonce = getNonce();

        const htmlPath = path.join(this.extensionUri.fsPath, 'out', 'webview', 'run-panel.html');
        let html = fs.readFileSync(htmlPath, 'utf-8');

        html = html.replace(/\{\{cspSource\}\}/g, webview.cspSource);
        html = html.replace(/\{\{nonce\}\}/g, nonce);
        html = html.replace(/\{\{cssUri\}\}/g, cssUri.toString());
        html = html.replace(/\{\{jsUri\}\}/g, jsUri.toString());

        return html;
    }
}
