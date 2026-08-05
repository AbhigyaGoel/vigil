# jobwatch one-shot setup for Windows.
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
# Generates your private notification topic, seeds the tracker, installs the
# background daemon to start at every logon, starts it now, and sends a test
# push. Requires Python 3.10+ on PATH. Re-running is safe (idempotent).
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host ""
Write-Host "jobwatch setup" -ForegroundColor Cyan
Write-Host "--------------"

# -- 1. find Python -----------------------------------------------------------
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) {
    Write-Host "Python not found on PATH. Install Python 3.10+ (python.org or Microsoft Store) and re-run." -ForegroundColor Red
    exit 1
}
$pyExe = & python -c "import sys; print(sys.executable)"
$pyw = Join-Path (Split-Path $pyExe) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $pyExe }   # fall back to console python
Write-Host "  Python: $pyExe"

# -- 2. private notification topic -------------------------------------------
$topic = $null
if (Test-Path .\config.local.json) {
    try { $topic = (Get-Content .\config.local.json -Raw | ConvertFrom-Json).ntfy_topic } catch {}
}
if (-not $topic -or $topic -like "CHANGE-ME*") {
    $topic = & python -c "import secrets; print('jobwatch-' + secrets.token_hex(5))"
    $json = '{' + "`n" + '  "ntfy_topic": "' + $topic + '"' + "`n" + '}'
    $json | Out-File -Encoding utf8 .\config.local.json
    Write-Host "  Generated private topic and wrote config.local.json"
} else {
    Write-Host "  Reusing existing topic from config.local.json"
}

# -- 3. seed ------------------------------------------------------------------
Write-Host "  Scanning sources (first run seeds silently, ~1 min)..."
& $pyExe .\watch.py --once

# -- 4. install autostart (Startup folder - no admin needed) ------------------
$startup = [Environment]::GetFolderPath("Startup")
$vbsPath = Join-Path $startup "jobwatch.vbs"
$watchPy = Join-Path $PSScriptRoot "watch.py"
$vbs = "' Auto-starts the jobwatch daemon at logon, hidden. Delete this file to disable.`r`n"
$vbs += 'Set s = CreateObject("WScript.Shell")' + "`r`n"
$vbs += ('s.Run """{0}"" ""{1}"" --watch", 0, False' -f $pyw, $watchPy) + "`r`n"
$vbs | Out-File -Encoding ascii $vbsPath
Write-Host "  Autostart installed: $vbsPath"

# -- 5. start the daemon now --------------------------------------------------
# (watch.py holds a localhost lock port, so double-starts are harmless)
Start-Process -FilePath wscript.exe -ArgumentList "`"$vbsPath`""
Start-Sleep -Seconds 3
Write-Host "  Daemon started (logs to watch.log)"

# -- 6. test push -------------------------------------------------------------
try {
    Invoke-RestMethod -Uri "https://ntfy.sh/$topic" -Method Post `
        -Body "jobwatch is live. New hardware internships will land here with an Apply button." `
        -Headers @{ Title = "jobwatch installed"; Tags = "zap"; Priority = "high" } | Out-Null
    Write-Host "  Test notification sent" -ForegroundColor Green
} catch {
    Write-Host "  Test push failed (check your network): $_" -ForegroundColor Yellow
}

# -- done ---------------------------------------------------------------------
Write-Host ""
Write-Host "Done. Two steps on your phone:" -ForegroundColor Cyan
Write-Host "  1. Install the ntfy app  (App Store / Play Store)"
Write-Host "  2. Subscribe to topic:   $topic" -ForegroundColor Green
Write-Host ""
Write-Host "Keep the topic private - anyone who knows it can see your alerts."
Write-Host "Tune filters in config.local.json. Watch live: Get-Content .\watch.log -Wait"
Write-Host ""
