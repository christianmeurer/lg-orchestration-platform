@echo off
setlocal

echo [py] uv sync
pushd py
uv sync
if errorlevel 1 exit /b 1

echo [py] pip-audit
uv run pip-audit
if errorlevel 1 exit /b 1

echo [py] ruff + mypy + pytest
uv run ruff format --check
if errorlevel 1 exit /b 1
uv run ruff check
if errorlevel 1 exit /b 1
uv run mypy
if errorlevel 1 exit /b 1
uv run pytest -q
if errorlevel 1 exit /b 1
popd

echo [rs] fmt + clippy + test
pushd rs
cargo install cargo-audit --locked
if errorlevel 1 exit /b 1
cargo audit
if errorlevel 1 exit /b 1
cargo fmt --check
if errorlevel 1 exit /b 1
cargo clippy --all-targets --all-features -- -D warnings
if errorlevel 1 exit /b 1
cargo test
if errorlevel 1 exit /b 1
popd

echo [eval] canary
pushd py
uv run python ..\eval\run.py
if errorlevel 1 exit /b 1
popd

echo [py] cli export-graph
pushd py
uv run lg-orch export-graph
if errorlevel 1 exit /b 1
popd

echo ok

