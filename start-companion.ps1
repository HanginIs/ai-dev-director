$ErrorActionPreference = 'Stop'
$directorRoot = $PSScriptRoot
$directorPython = (Get-Command python).Source
try {
    $directorHealth = Invoke-RestMethod 'http://127.0.0.1:8081/ops/state' -TimeoutSec 3
    if ($directorHealth.solomon_url) {
        Write-Output 'AI DEV companion already running'
        Push-Location $directorRoot; try { Write-Output ('Login link (once per browser): ' + (& $directorPython companion_server.py --login-url)) } finally { Pop-Location }
        exit 0
    }
} catch { }
# The link is produced first: it creates data/solomon-bridge.token, which the server then reads.
Push-Location $directorRoot; try { $directorLink = (& $directorPython companion_server.py --login-url) } finally { Pop-Location }
Start-Process -FilePath $directorPython -ArgumentList 'companion_server.py' -WorkingDirectory $directorRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $directorRoot 'data/companion.log') -RedirectStandardError (Join-Path $directorRoot 'data/companion.err') | Out-Null
Write-Output 'AI DEV companion started'
Write-Output ('Login link (once per browser): ' + $directorLink)
