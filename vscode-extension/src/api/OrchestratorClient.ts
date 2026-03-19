import * as http from 'http';
import * as https from 'https';
import { randomUUID } from 'crypto';

export interface RunSummary {
  run_id: string;
  status: string;
  request: string;
  started_at: string;
  cancellable: boolean;
  pending_approval: boolean;
}

export interface RunDetail {
  run_id: string;
  status: string;
  request: string;
  started_at: string;
  exit_code: number | null;
  cancel_requested: boolean;
  cancellable: boolean;
  trace_path: string | null;
  trace_ready: boolean;
  trace: unknown;
  pending_approval: boolean;
  pending_approval_summary: string;
  approval_history: unknown[];
  checkpoint_id: string | null;
  thread_id: string | null;
  verification: unknown;
}

export interface RunEvent {
  type: string;
  run_id?: string;
  node?: string;
  message?: string;
  patch?: string;
  diff?: string;
  status?: string;
  data?: unknown;
}

export class OrchestratorClient {
  private readonly baseUrl: string;
  private readonly bearerToken: string | null;

  public constructor(baseUrl: string, bearerToken: string | null = null) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.bearerToken = bearerToken;
  }

  public async getRuns(): Promise<RunSummary[]> {
    const result = await this.requestJson<RunSummary[] | { runs?: RunSummary[] }>(
      'GET',
      `${this.baseUrl}/v1/runs`,
    );
    if (Array.isArray(result)) {
      return result;
    }
    const typed = result as { runs?: RunSummary[] };
    return typed.runs ?? [];
  }

  public async getRunDetail(runId: string): Promise<RunDetail> {
    return this.requestJson<RunDetail>(
      'GET',
      `${this.baseUrl}/v1/runs/${encodeURIComponent(runId)}`,
    );
  }

  public async approveRun(runId: string): Promise<void> {
    await this.requestJson<unknown>(
      'POST',
      `${this.baseUrl}/v1/runs/${encodeURIComponent(runId)}/approve`,
      { actor: 'vscode' },
    );
  }

  public async rejectRun(runId: string): Promise<void> {
    await this.requestJson<unknown>(
      'POST',
      `${this.baseUrl}/v1/runs/${encodeURIComponent(runId)}/reject`,
      { actor: 'vscode' },
    );
  }

  public async cancelRun(runId: string): Promise<void> {
    await this.requestJson<unknown>(
      'POST',
      `${this.baseUrl}/v1/runs/${encodeURIComponent(runId)}/cancel`,
      {},
    );
  }

  public async postRun(request: string, view: string = 'classic'): Promise<RunDetail> {
    return this.requestJson<RunDetail>('POST', `${this.baseUrl}/v1/runs`, { request, view });
  }

  public async getLogs(runId: string): Promise<{ logs?: unknown }> {
    return this.requestJson<{ logs?: unknown }>(
      'GET',
      `${this.baseUrl}/v1/runs/${encodeURIComponent(runId)}/logs`,
    );
  }

  public async healthz(): Promise<void> {
    await this.requestJson<unknown>('GET', `${this.baseUrl}/healthz`);
  }

  public streamRun(
    runId: string,
    onEvent: (event: RunEvent) => void,
    onDone: () => void,
  ): () => void {
    const streamUrl = `${this.baseUrl}/v1/runs/${encodeURIComponent(runId)}/stream`;
    const url = new URL(streamUrl);
    const client = url.protocol === 'https:' ? https : http;

    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
      'Cache-Control': 'no-cache',
      'X-Request-ID': randomUUID(),
    };
    if (this.bearerToken) {
      headers.Authorization = `Bearer ${this.bearerToken}`;
    }

    let done = false;
    const req = client.request(
      {
        hostname: url.hostname,
        port: url.port,
        path: `${url.pathname}${url.search}`,
        method: 'GET',
        headers,
      },
      (response) => {
        let buffer = '';
        response.on('data', (chunk: Buffer | string) => {
          buffer += Buffer.isBuffer(chunk) ? chunk.toString('utf8') : chunk;
          const parts = buffer.split('\n\n');
          buffer = parts.pop() ?? '';
          for (const part of parts) {
            const lines = part.split('\n');
            let data = '';
            for (const line of lines) {
              if (line.startsWith('data:')) {
                data += line.slice(5).trim();
              }
            }
            if (!data) {
              continue;
            }
            if (data === 'done') {
              if (!done) {
                done = true;
                onDone();
              }
              req.destroy();
              return;
            }
            try {
              const event = JSON.parse(data) as RunEvent;
              onEvent(event);
            } catch {
              // ignore malformed SSE frames
            }
          }
        });
        response.on('end', () => {
          if (!done) {
            done = true;
            onDone();
          }
        });
      },
    );

    req.on('error', () => {
      if (!done) {
        done = true;
        onDone();
      }
    });
    req.end();

    return () => {
      if (!done) {
        done = true;
        req.destroy();
      }
    };
  }

  private async requestJson<T>(
    method: 'GET' | 'POST',
    requestUrl: string,
    body?: unknown,
  ): Promise<T> {
    const url = new URL(requestUrl);
    const client =
      url.protocol === 'https:' ? https : url.protocol === 'http:' ? http : null;
    if (!client) {
      throw new Error(`unsupported protocol: ${url.protocol}`);
    }

    const payload = body === undefined ? undefined : JSON.stringify(body);
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'X-Request-ID': randomUUID(),
    };
    if (this.bearerToken) {
      headers.Authorization = `Bearer ${this.bearerToken}`;
    }
    if (payload !== undefined) {
      headers['Content-Type'] = 'application/json';
      headers['Content-Length'] = Buffer.byteLength(payload).toString();
    }

    return new Promise<T>((resolve, reject) => {
      const req = client.request(
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
              reject(
                new Error(
                  `invalid JSON: ${error instanceof Error ? error.message : String(error)}`,
                ),
              );
            }
          });
        },
      );

      req.setTimeout(30_000, () => req.destroy(new Error('request timed out')));
      req.on('error', (error: Error) => reject(error));
      if (payload !== undefined) {
        req.write(payload);
      }
      req.end();
    });
  }
}
