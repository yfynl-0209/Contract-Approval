# Start all 5 local services, each in its own window:
#   mock gateway (8001) -> API (8000) -> worker / outbox dispatcher -> vite (5173)
# Usage (project root):  .\scripts\start_all.ps1
# Stop:                  .\scripts\stop_all.ps1   (or Ctrl+C per window)
# Detailed guide (Chinese): .\startguide.md

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

# ---- 0. environment self-check ----
if (-not (Test-Path (Join-Path $root '.env'))) {
    Copy-Item (Join-Path $root '.env.example') (Join-Path $root '.env')
    Write-Host '[env] created .env from .env.example (dev defaults)' -ForegroundColor Yellow
}
if (-not (Test-Path (Join-Path $root 'data\app.db'))) {
    Write-Host '[db]  data/app.db not found, running init_db...' -ForegroundColor Yellow
    & $py (Join-Path $root 'scripts\init_db.py')
}

# ---- 1~4. backend processes (separate windows, per-service logs) ----
$services = @(
    @{ Name = '(1/5) mock gateway  :8001'; Exe = $py; Args = '-m uvicorn mock_approval.main:app --host 127.0.0.1 --port 8001' },
    @{ Name = '(2/5) API backend   :8000'; Exe = $py; Args = '-m uvicorn app.main:app --host 127.0.0.1 --port 8000' },
    @{ Name = '(3/5) parse/rule worker'; Exe = $py; Args = 'scripts/run_worker.py' },
    @{ Name = '(4/5) outbox dispatcher'; Exe = $py; Args = 'scripts/run_outbox_dispatcher.py' }
)
foreach ($s in $services) {
    Start-Process -FilePath $s.Exe -ArgumentList $s.Args -WorkingDirectory $root -WindowStyle Minimized
    Write-Host "[start] $($s.Name)" -ForegroundColor Green
}

# ---- 5. frontend (wrap npm in cmd so PATH resolves) ----
Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', 'cd /d frontend && npm run dev' -WorkingDirectory $root -WindowStyle Minimized
Write-Host '[start] (5/5) frontend vite :5173' -ForegroundColor Green

# ---- wait for ports ----
function Wait-Port([int]$Port, [string]$Label, [int]$Seconds = 30) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
            Write-Host "[ok]    $Label -> http://127.0.0.1:$Port" -ForegroundColor Green
            return
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "[fail]  $Label not listening on $Port within ${Seconds}s - check its window" -ForegroundColor Red
}

Wait-Port 8001 'mock gateway'
Wait-Port 8000 'API backend'
Wait-Port 5173 'frontend'

Write-Host ''
Write-Host 'Open the console:  http://127.0.0.1:5173' -ForegroundColor Cyan
Write-Host 'Switch identity in the top-right panel (AUTH_MODE=dev, header-based).'
