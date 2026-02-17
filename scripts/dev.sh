#!/usr/bin/env bash
set -euo pipefail

(
  cd py
  uv sync
  uv run pip-audit
  uv run ruff format --check
  uv run ruff check
  uv run mypy
  uv run pytest -q
)

(
  cd rs
  cargo install cargo-audit --locked
  cargo audit
  cargo fmt --check
  cargo clippy --all-targets --all-features -- -D warnings
  cargo test
)

uv run python eval/run.py

echo ok
