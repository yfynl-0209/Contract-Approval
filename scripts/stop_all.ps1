# Stop every local service of this project (matched by port + script name).
# Usage: .\scripts\stop_all.ps1

$stopped = @()

# 1) listeners on our ports (frontend 5173 / API 8000 / mock gateway 8001)
foreach ($port in 5173, 8000, 8001) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
            $proc = Get-Process -Id $_ -ErrorAction SilentlyContinue
            if ($proc) {
                Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
                $stopped += "$($proc.ProcessName) (pid $_, port $port)"
            }
        }
}

# 2) workers / dispatcher by command line (no fixed port)
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match 'run_worker\.py|run_outbox_dispatcher\.py' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped += "worker (pid $($_.ProcessId))"
    }

if ($stopped.Count -eq 0) {
    Write-Host 'Nothing running.' -ForegroundColor Yellow
} else {
    $stopped | Sort-Object -Unique | ForEach-Object { Write-Host "[stop] $_" -ForegroundColor Green }
    Write-Host "Stopped $($stopped.Count) process(es)." -ForegroundColor Cyan
}
