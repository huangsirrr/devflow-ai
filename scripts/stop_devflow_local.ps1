<#
    DevFlow AI - stop the local stack started by start_devflow_local.ps1.
#>
param(
    [int[]]$Ports = @(8000, 3000)
)

foreach ($port in $Ports) {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $connections) { Write-Host "port $port : not listening"; continue }
    foreach ($processId in ($connections.OwningProcess | Sort-Object -Unique)) {
        try {
            $proc = Get-Process -Id $processId -ErrorAction Stop
            Stop-Process -Id $processId -Force
            Write-Host "port $port : stopped PID $processId ($($proc.ProcessName))"
        }
        catch { Write-Host "port $port : could not stop PID $processId - $($_.Exception.Message)" }
    }
}
