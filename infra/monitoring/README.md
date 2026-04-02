# Lula Operations — Grafana Dashboard

This directory contains a Grafana dashboard template for monitoring Lula's
Prometheus metrics.

## Import instructions

1. Open your Grafana instance and navigate to **Dashboards → Import**.
2. Click **Upload JSON file** and select `grafana-dashboard.json`.
3. On the import screen, select your Prometheus datasource for the
   `DS_PROMETHEUS` input.
4. Click **Import**.

The dashboard will appear under the `lula` and `operations` tags.

## Panels

| # | Title | Query | Visualization |
|---|-------|-------|---------------|
| 1 | Tool Call Rate | `rate(runner_tool_calls_total[5m])` by `tool` | Time series |
| 2 | Tool Latency p95 | `histogram_quantile(0.95, rate(runner_tool_duration_seconds_bucket[5m]))` | Time series |
| 3 | Sandbox Tier Distribution | `runner_sandbox_tier` by `tier` | Pie chart |
| 4 | Run Rate | `rate(lula_runs_total[5m])` by `status` | Time series |
| 5 | Active Runs | `lula_runs_total{status="running"}` | Stat |
| 6 | cgroup Availability | `runner_cgroup_available` | Stat |

## Required metrics

The following Prometheus metrics must be exposed by your Lula services:

- `runner_tool_calls_total` — counter with `tool` label
- `runner_tool_duration_seconds` — histogram with `tool` label
- `runner_sandbox_tier` — gauge with `tier` label
- `lula_runs_total` — counter with `status` label
- `runner_cgroup_available` — gauge (0 or 1)
