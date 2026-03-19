import * as vscode from 'vscode';
import { OrchestratorClient } from './api/OrchestratorClient';

export class RunPanelProvider {
  private readonly panels = new Map<string, vscode.WebviewPanel>();

  public openPanel(
    runId: string,
    _client: OrchestratorClient,
    context: vscode.ExtensionContext,
  ): void {
    const existing = this.panels.get(runId);
    if (existing) {
      existing.reveal();
      return;
    }

    const panel = vscode.window.createWebviewPanel(
      'orchestratorRun',
      `Run: ${runId}`,
      vscode.ViewColumn.One,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
      },
    );

    panel.webview.html = this.renderHtml(runId);

    panel.onDidDispose(
      () => {
        this.panels.delete(runId);
      },
      undefined,
      context.subscriptions,
    );

    this.panels.set(runId, panel);
  }

  private resolveBaseUrl(): string {
    const configured = vscode.workspace
      .getConfiguration('lula')
      .get<string>('remoteApiBaseUrl', '')
      .trim();
    if (configured) {
      return configured.replace(/\/+$/, '');
    }
    return 'http://localhost:8765';
  }

  private renderHtml(runId: string): string {
    const escaped = escapeHtml(runId);
    const baseUrl = this.resolveBaseUrl();
    const browserUrl = `${baseUrl}/runs/${encodeURIComponent(runId)}`;

    return `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta
      http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline';"
    />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Run ${escaped}</title>
    <style>
      :root { color-scheme: light dark; }
      body {
        font-family: var(--vscode-font-family);
        margin: 0;
        padding: 24px;
        display: flex;
        flex-direction: column;
        align-items: flex-start;
        gap: 16px;
      }
      h2 { margin: 0; font-size: 14px; }
      .run-id {
        font-family: var(--vscode-editor-font-family, monospace);
        font-size: 12px;
        color: var(--vscode-descriptionForeground);
        word-break: break-all;
      }
      .hint {
        font-size: 12px;
        color: var(--vscode-descriptionForeground);
        margin: 0;
      }
      a {
        color: var(--vscode-textLink-foreground);
        font-size: 13px;
      }
    </style>
  </head>
  <body>
    <h2>Orchestrator Run</h2>
    <div class="run-id">${escaped}</div>
    <p class="hint">
      The run console is served by the local orchestrator SPA.
      Click the link below to open it in your browser.
    </p>
    <a href="${escapeHtml(browserUrl)}" target="_blank" rel="noopener noreferrer">
      Open run in browser
    </a>
  </body>
</html>`;
  }
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
