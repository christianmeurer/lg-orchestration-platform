@echo off
setlocal

set "ROOT=%~dp0.."
set "RUNNER_BIND=127.0.0.1:8088"
set "RUNNER_API_KEY=dev-insecure"

if "%LG_PROFILE%"=="" set "LG_PROFILE=dev"

if "%~1"=="" (
  set "REQUEST_ARGS="implement a small feature and verify tests""
  set "REQUEST_LOG=implement a small feature and verify tests"
) else (
  set "REQUEST_ARGS=%*"
  set "REQUEST_LOG=%*"
)

echo [bootstrap] root: %ROOT%
echo [bootstrap] profile: %LG_PROFILE%
echo [bootstrap] request: %REQUEST_LOG%

if "%MODEL_ACCESS_KEY%"=="" (
  if "%DIGITAL_OCEAN_MODEL_ACCESS_KEY%"=="" (
    echo [bootstrap] WARNING: MODEL_ACCESS_KEY is not set. Planner will use deterministic fallback.
  )
)

echo [bootstrap] starting runner on %RUNNER_BIND%
start "lg-runner" cmd /c "cd /d %ROOT%\rs\runner && cargo run -- --bind %RUNNER_BIND% --root-dir %ROOT% --profile %LG_PROFILE% --api-key %RUNNER_API_KEY%"

echo [bootstrap] waiting for runner health endpoint...
powershell -NoProfile -Command "$ok=$false; for($i=0; $i -lt 60; $i++){ try { $r=Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8088/healthz -TimeoutSec 2; if($r.StatusCode -eq 200){ $ok=$true; break } } catch {}; Start-Sleep -Milliseconds 500 }; if(-not $ok){ exit 1 }"
if errorlevel 1 (
  echo [bootstrap] ERROR: runner did not become healthy on /healthz.
  exit /b 1
)

echo [bootstrap] syncing python dependencies
pushd "%ROOT%\py"
uv sync
if errorlevel 1 (
  popd
  exit /b 1
)

echo [bootstrap] running orchestrator
uv run lg-orch run %REQUEST_ARGS% --trace
set "RC=%ERRORLEVEL%"
popd

exit /b %RC%

