@echo off
REM Build the Firecracker guest rootfs ext4 image containing lula-guest-agent.
REM Output: artifacts\rootfs.ext4
REM Requires: Docker Desktop for Windows with BuildKit enabled.
REM
REM This script is idempotent: re-running overwrites the previous image.

SET SCRIPT_DIR=%~dp0
SET REPO_ROOT=%SCRIPT_DIR%..
SET OUTPUT_DIR=%REPO_ROOT%\artifacts

where docker >nul 2>&1
IF ERRORLEVEL 1 (
    echo ERROR: docker not found in PATH. Install Docker Desktop and ensure it is running.
    exit /b 1
)

IF NOT EXIST "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

echo Building guest rootfs (this may take several minutes on first run)...
SET DOCKER_BUILDKIT=1
docker build ^
  --file "%REPO_ROOT%\rs\guest-agent\Dockerfile.rootfs" ^
  --target export ^
  --output "type=local,dest=%OUTPUT_DIR%" ^
  "%REPO_ROOT%\rs"

IF ERRORLEVEL 1 (
    echo ERROR: Docker build failed.
    echo   Ensure Docker Desktop is running and BuildKit is enabled.
    exit /b 1
)

IF NOT EXIST "%OUTPUT_DIR%\rootfs.ext4" (
    echo ERROR: Expected output file '%OUTPUT_DIR%\rootfs.ext4' not found after build.
    echo   Ensure Docker BuildKit is enabled and the build completed without errors.
    exit /b 1
)

echo Guest rootfs built: %OUTPUT_DIR%\rootfs.ext4
