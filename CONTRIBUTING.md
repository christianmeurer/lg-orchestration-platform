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
cd py && uv run pytest --cov=lg_orch --cov-fail-under=75
```

The current gate is 75%. Do not submit PRs that reduce coverage below this threshold.

## Commit discipline

- Keep changes small and reviewable.
- Add/extend tests where behavior changes.
- Update docs when interfaces change.
