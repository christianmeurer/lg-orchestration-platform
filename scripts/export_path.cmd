@echo off
setlocal

set "ROOT=%~dp0.."
set "BIN=%ROOT%\py\.venv\Scripts"

if not exist "%BIN%\lg-orch.exe" (
  echo [path] lg-orch.exe not found in "%BIN%"
  echo [path] creating virtualenv + scripts with: uv sync
  pushd "%ROOT%\py"
  uv sync
  if errorlevel 1 (
    popd
    exit /b 1
  )
  popd
)

set "PATH=%BIN%;%PATH%"
echo [path] session PATH updated: %BIN%

powershell -NoProfile -ExecutionPolicy Bypass -Command "$bin=[System.IO.Path]::GetFullPath('%BIN%'); $userPath=[Environment]::GetEnvironmentVariable('Path','User'); if([string]::IsNullOrWhiteSpace($userPath)){ $newPath=$bin } elseif(($userPath -split ';') -contains $bin){ $newPath=$userPath } else { $newPath=$userPath + ';' + $bin }; [Environment]::SetEnvironmentVariable('Path',$newPath,'User')"
if errorlevel 1 (
  echo [path] failed to update persistent user PATH
  exit /b 1
)

echo [path] persistent user PATH updated
echo [path] open a new terminal and run: lg-orch --help

exit /b 0

