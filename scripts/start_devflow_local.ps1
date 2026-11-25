<#
    DevFlow AI - local startup (SQLite + deterministic embedding, no Docker required).

    Environment for this checkout:
      backend  : http://127.0.0.1:8000  (FastAPI / uvicorn)
      frontend : http://127.0.0.1:3000  (Next.js)

    Usage:
      .\scripts\start_devflow_local.ps1              # production build + next start
      .\scripts\start_devflow_local.ps1 -Dev         # next dev (hot reload)
      .\scripts\stop_devflow_local.ps1

    Note: -Dev runs the Next.js dev server, which forks child processes. Run this
    script from a normal PowerShell window (not from inside a restricted sandbox).
#>
param(
    [switch]$Dev,
    [ValidateRange(1, 65535)][int]$BackendPort = 8000,
    [ValidateRange(1, 65535)][int]$FrontendPort = 3000
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$backendDirectory = Join-Path $projectRoot "backend"
$frontendDirectory = Join-Path $projectRoot "frontend"
$pythonPath = Join-Path $backendDirectory ".venv\Scripts\python.exe"
$dataDirectory = Join-Path $projectRoot ".data"
$nodeModules = Join-Path $frontendDirectory "node_modules"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "backend/.venv not found. Create it and install backend/requirements.txt first."
}
if (-not (Test-Path -LiteralPath $nodeModules -PathType Container)) {
    throw "frontend/node_modules not found. Run npm install in frontend/ first."
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot ".env") -PathType Leaf)) {
    throw ".env not found at $projectRoot\.env"
}

New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null

$backendOut = Join-Path $dataDirectory "devflow-backend.out.log"
$backendErr = Join-Path $dataDirectory "devflow-backend.err.log"
$frontendOut = Join-Path $dataDirectory "devflow-frontend.out.log"
$frontendErr = Join-Path $dataDirectory "devflow-frontend.err.log"

$env:PYTHONPATH = $backendDirectory
$env:PYTHONUNBUFFERED = "1"
$env:NEXT_TELEMETRY_DISABLED = "1"

Write-Host "Starting backend on port $BackendPort ..."
$backendProcess = Start-Process -FilePath $pythonPath `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$BackendPort") `
    -WorkingDirectory $backendDirectory `
    -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr `
    -WindowStyle Hidden -PassThru

$backendReady = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    if ($backendProcess.HasExited) { throw "Backend exited early. See $backendErr" }
    try { Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/health" -TimeoutSec 2 | Out-Null; $backendReady = $true; break }
    catch { Start-Sleep -Milliseconds 500 }
}
if (-not $backendReady) { throw "Backend not ready within 30s. See $backendErr" }
Write-Host "  backend ready: http://127.0.0.1:$BackendPort/health"

$nextCli = Join-Path $frontendDirectory "node_modules\next\dist\bin\next"
if ($Dev) {
    $frontendArgs = @($nextCli, "dev", "-p", "$FrontendPort")
    $frontendLabel = "next dev"
}
else {
    if (-not (Test-Path -LiteralPath (Join-Path $frontendDirectory ".next\BUILD_ID"))) {
        Write-Host "No production build found - running 'next build' first ..."
        Push-Location $frontendDirectory
        try { & node $nextCli build; if ($LASTEXITCODE -ne 0) { throw "next build failed" } }
        finally { Pop-Location }
    }
    $frontendArgs = @($nextCli, "start", "-p", "$FrontendPort")
    $frontendLabel = "next start"
}

Write-Host "Starting frontend ($frontendLabel) on port $FrontendPort ..."
$frontendProcess = Start-Process -FilePath "node" -ArgumentList $frontendArgs `
    -WorkingDirectory $frontendDirectory `
    -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr `
    -WindowStyle Hidden -PassThru

$frontendReady = $false
for ($attempt = 0; $attempt -lt 120; $attempt++) {
    if ($frontendProcess.HasExited) { throw "Frontend exited early. See $frontendErr" }
    try { Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort" -TimeoutSec 2 | Out-Null; $frontendReady = $true; break }
    catch { Start-Sleep -Milliseconds 500 }
}
if (-not $frontendReady) { throw "Frontend not ready within 60s. See $frontendErr" }

Write-Host ""
Write-Host "DevFlow AI is running:"
Write-Host "  frontend : http://127.0.0.1:$FrontendPort"
Write-Host "  backend  : http://127.0.0.1:$BackendPort"
Write-Host "  API docs : http://127.0.0.1:$BackendPort/docs"
Write-Host "  backend PID  : $($backendProcess.Id)"
Write-Host "  frontend PID : $($frontendProcess.Id)"
Write-Host ""
Write-Host "Logs: $backendOut / $frontendOut"
Write-Host "Stop: .\scripts\stop_devflow_local.ps1"
