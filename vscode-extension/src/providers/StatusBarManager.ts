import * as vscode from 'vscode';
import type { RunSummary } from '../api/OrchestratorClient';

export class StatusBarManager {
  private readonly item: vscode.StatusBarItem;

  public constructor() {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 10);
    this.item.command = 'lgOrch.openPanel';
    this.item.text = '$(hubot) Lula';
    this.item.tooltip = 'Open Lula Panel';
    this.item.show();
  }

  public update(run: RunSummary | null): void {
    if (!run) {
      this.item.text = '$(hubot) Lula';
      this.item.tooltip = 'Open Lula Panel';
      this.item.backgroundColor = undefined;
      return;
    }

    const shortId = run.run_id.slice(0, 8);

    switch (run.status) {
      case 'running':
      case 'starting':
      case 'queued':
        this.item.text = '$(sync~spin) Lula: Running';
        this.item.tooltip = `${shortId} — ${run.status}`;
        this.item.backgroundColor = undefined;
        break;
      case 'succeeded':
        this.item.text = '$(check) Lula: Done';
        this.item.tooltip = `${shortId} — succeeded`;
        this.item.backgroundColor = undefined;
        break;
      case 'failed':
      case 'cancelled':
        this.item.text = '$(error) Lula: Failed';
        this.item.tooltip = `${shortId} — ${run.status}`;
        this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
        break;
      case 'suspended':
        this.item.text = '$(warning) Lula: Suspended';
        this.item.tooltip = `${shortId} — suspended (approval needed)`;
        this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
        break;
      default:
        this.item.text = `$(hubot) Lula: ${run.status}`;
        this.item.tooltip = `${shortId} — ${run.status}`;
        this.item.backgroundColor = undefined;
        break;
    }
  }

  public dispose(): void {
    this.item.hide();
    this.item.dispose();
  }
}
