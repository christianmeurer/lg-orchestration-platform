$ErrorActionPreference = "Stop"

Write-Host "[py] uv sync"
Push-Location py
uv sync

Write-Host "[py] pip-audit"
uv run pip-audit
Pop-Location

Write-Host "[py] ruff + mypy + pytest"
Push-Location py
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest -q
Pop-Location

Write-Host "[rs] fmt + clippy + test"
Push-Location rs
cargo install cargo-audit --locked
cargo audit
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test
Pop-Location

Write-Host "[eval] canary"
uv run python eval/run.py

Write-Host "ok"

