@echo off
setlocal

:: do_deploy.cmd — Windows wrapper for scripts/do_deploy.sh
:: Requires WSL, Git Bash, or any bash available on PATH.

where bash >nul 2>nul
if errorlevel 1 (
  echo [error] bash is not available on PATH. Install Git for Windows or WSL. 1>&2
  exit /b 1
)

bash "%~dp0do_deploy.sh" %*
