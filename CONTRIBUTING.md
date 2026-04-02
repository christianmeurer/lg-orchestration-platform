# Contributing

## Development

- Windows: [`scripts/dev.cmd`](scripts/dev.cmd:1)
- PowerShell: [`scripts/dev.ps1`](scripts/dev.ps1:1)
- Bash: [`scripts/dev.sh`](scripts/dev.sh:1)

## Build Tools

### Python orchestrator

The Python side uses [`uv`](https://github.com/astral-sh/uv) as its package manager:

```bash
cd py
uv sync --dev       # install all dependencies
uv run pytest       # run tests
uv run ruff check . # lint
```

The CLI uses the `rich` library for formatted terminal output (panels, tables, colored logs). Log output goes to stderr; structured results go to stdout.

### Leptos SPA (Rust/WASM)

The web frontend is a Leptos application built with [Trunk](https://trunkrs.dev/):

```bash
cd rs/spa-leptos
trunk serve              # dev server with hot-reload
trunk build --release    # production build to dist/
```

Prerequisites: Rust with `wasm32-unknown-unknown` target (`rustup target add wasm32-unknown-unknown`) and Trunk (`cargo install trunk`).

### VS Code Extension

The extension is bundled with esbuild:

```bash
cd vscode-extension
npm install
node esbuild.js          # development build
npx vsce package         # produce .vsix for distribution
```

### Rust Runner

```bash
cd rs
cargo build              # debug build
cargo test --all-features
cargo clippy --all-targets --all-features -- -D warnings
```

## Code quality

- Python: `ruff format --check`, `ruff check`, `mypy`, `pytest`
- Rust: `cargo fmt --check`, `cargo clippy ... -D warnings`, `cargo test`

## Coverage gate

Tests must pass the coverage threshold enforced in `pyproject.toml`:

```bash
cd py && uv run pytest --cov=lg_orch --cov-fail-under=84
```

The current gate is 84%. Do not submit PRs that reduce coverage below this threshold.

## Deployment validation

After deploying to Kubernetes, run the verification script to confirm all pods, ingress, and health endpoints are functional:

```bash
bash scripts/verify-deployment.sh
```

## Testing GLEAN

To test with the GLEAN verification framework active, set the environment variable before running:

```bash
LG_GLEAN_ENABLED=true cd py && uv run pytest
```

GLEAN adds pre- and post-tool guideline checks to the executor. Tests that exercise the executor node will validate GLEAN's veto and audit paths when this flag is set.

## Commit discipline

- Keep changes small and reviewable.
- Add/extend tests where behavior changes.
- Update docs when interfaces change.
