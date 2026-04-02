# Lula Documentation Index

## Core

| File | Description |
|---|---|
| [architecture.md](architecture.md) | System architecture: Rust runner, Python orchestrator, sandbox layers, K8s deployment |
| [quality_report.md](quality_report.md) | Full codebase audit — findings, fixes applied, and component scores |

## Deployment

| File | Description |
|---|---|
| [deployment_digitalocean.md](deployment_digitalocean.md) | DigitalOcean Kubernetes deployment guide |
| [deployment-edge.md](deployment-edge.md) | Edge / local air-gapped deployment guide (k3s + Ollama) |
| [deployment_plan.md](deployment_plan.md) | Deployment planning notes and rollout strategy |
| [digitalocean_troubleshooting_2026.md](digitalocean_troubleshooting_2026.md) | Troubleshooting the gVisor empty-output bug and related DOKS issues |
| [registry_setup.md](registry_setup.md) | Container registry setup (DOCR) |
| [signing-setup.md](signing-setup.md) | Image signing setup (cosign / Sigstore) |
| [gitops.md](gitops.md) | GitOps workflow with FluxCD or ArgoCD |

## Design & Planning

| File | Description |
|---|---|
| [improvement_plan.md](improvement_plan.md) | Incremental improvement plan for the orchestrator and runner |
| [langgraph_plan.md](langgraph_plan.md) | LangGraph orchestration design decisions |
| [market_maturity_analysis.md](market_maturity_analysis.md) | Competitive analysis vs Devin, SWE-agent, OpenHands, Aider, Claude Code |
| [platform_console.md](platform_console.md) | Platform console / operator UI design |
| [sota_2026_plan.md](sota_2026_plan.md) | State-of-the-art 2026 feature roadmap |
| [agent_collaboration_2026.md](agent_collaboration_2026.md) | Multi-agent collaboration design and SOTA 2026 direction |
| [wave7_spa_sse.md](wave7_spa_sse.md) | Wave 7 — SPA + SSE streaming implementation notes |

## Superpowers (Specs & Plans)

| File | Description |
|---|---|
| [superpowers/specs/2026-04-01-spa-overhaul-design.md](superpowers/specs/2026-04-01-spa-overhaul-design.md) | Design spec for the Leptos SPA overhaul |
| [superpowers/specs/2026-04-01-vscode-extension-design.md](superpowers/specs/2026-04-01-vscode-extension-design.md) | Design spec for the VS Code extension |
| [superpowers/plans/2026-04-01-spa-overhaul.md](superpowers/plans/2026-04-01-spa-overhaul.md) | Implementation plan for the Leptos SPA overhaul |
| [superpowers/plans/2026-04-01-vscode-extension.md](superpowers/plans/2026-04-01-vscode-extension.md) | Implementation plan for the VS Code extension |
