import * as vscode from 'vscode';
import { OrchestratorClient } from './api/OrchestratorClient';

export class RunItem extends vscode.TreeItem {
  public readonly runId: string;
  public readonly status: string;
  public readonly task: string;

  public constructor(runId: string, status: string, task: string) {
    const label = task.length > 60 ? task.slice(0, 60) : task;
    super(label, vscode.TreeItemCollapsibleState.None);

    this.runId = runId;
    this.status = status;
    this.task = task;
    this.description = status;
    this.iconPath = RunItem.iconForStatus(status);
    this.contextValue = 'orchestratorRun';
    this.tooltip = `${runId}\n${task}`;
    this.command = {
      command: 'lula.openRunPanel',
      title: 'Open Run Panel',
      arguments: [runId]
    };
  }

  private static iconForStatus(status: string): vscode.ThemeIcon {
    switch (status) {
      case 'succeeded':
        return new vscode.ThemeIcon('pass');
      case 'failed':
        return new vscode.ThemeIcon('error');
      case 'running':
      case 'starting':
      case 'queued':
        return new vscode.ThemeIcon('sync~spin');
      case 'suspended':
        return new vscode.ThemeIcon('warning', new vscode.ThemeColor('list.warningForeground'));
      default:
        return new vscode.ThemeIcon('circle-outline');
    }
  }
}

export class RunTreeProvider implements vscode.TreeDataProvider<RunItem> {
  private readonly _onDidChangeTreeData = new vscode.EventEmitter<RunItem | undefined | void>();
  public readonly onDidChangeTreeData: vscode.Event<RunItem | undefined | void> =
    this._onDidChangeTreeData.event;

  private refreshTimer: ReturnType<typeof setInterval> | undefined;

  public constructor(private readonly client: OrchestratorClient) {
    // Auto-refresh every 10 seconds
    this.refreshTimer = setInterval(() => {
      this._onDidChangeTreeData.fire();
    }, 10_000);
  }

  public getTreeItem(element: RunItem): vscode.TreeItem {
    return element;
  }

  public async getChildren(): Promise<RunItem[]> {
    let summaries: Awaited<ReturnType<OrchestratorClient['getRuns']>>;
    try {
      summaries = await this.client.getRuns();
    } catch {
      return [];
    }

    const pendingCount = summaries.filter(s => s.pending_approval).length;
    if (pendingCount > 0) {
      // Update the tree view description via the view
      // This is informational - the description is set on the view itself
    }

    return summaries.map(
      (s) => new RunItem(s.run_id, s.status, s.request),
    );
  }

  public refresh(): void {
    this._onDidChangeTreeData.fire();
  }

  public dispose(): void {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = undefined;
    }
    this._onDidChangeTreeData.dispose();
  }
}
