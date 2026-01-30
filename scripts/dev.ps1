$ErrorActionPreference = "Stop"

Write-Host "[py] uv sync"
Push-Location py
uv sync
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
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test
Pop-Location

Write-Host "[eval] canary"
uv run python eval/run.py

Write-Host "ok"

