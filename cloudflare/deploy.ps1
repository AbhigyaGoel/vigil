# One-shot deploy of the vigil instant tier to Cloudflare (free plan).
# Prereq (once): npx wrangler login   <- opens browser, sign in / create account
# Then:          powershell -ExecutionPolicy Bypass -File .\deploy.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host ""
Write-Host "vigil instant-tier deploy" -ForegroundColor Cyan
Write-Host "----------------------------"

# -- 1. dedupe-memory KV namespace --------------------------------------------
# Look up (or create) the namespace on YOUR account, then pin its id into
# wrangler.toml - so a fresh fork never inherits someone else's id.
$id = $null
try {
    $spaces = npx wrangler kv namespace list 2>$null | ConvertFrom-Json
    $mine = $spaces | Where-Object { $_.title -like "*SEEN*" } | Select-Object -First 1
    if ($mine) { $id = $mine.id; Write-Host "  Reusing KV namespace: $id" }
} catch {}
if (-not $id) {
    Write-Host "  Creating KV namespace..."
    $out = npx wrangler kv namespace create SEEN 2>&1 | Out-String
    if ($out -match '"?id"?\s*[=:]\s*"([0-9a-f]{32})"') {
        $id = $Matches[1]
        Write-Host "  KV namespace ready: $id"
    } else {
        Write-Host "  Could not parse KV id from wrangler output:" -ForegroundColor Red
        Write-Host $out
        exit 1
    }
}
$toml = Get-Content .\wrangler.toml -Raw
$toml -replace 'id = "(REPLACE_WITH_KV_ID|[0-9a-f]{32})"', "id = `"$id`"" | Set-Content .\wrangler.toml -Encoding utf8

# -- 2. deploy the worker + 1-minute cron -------------------------------------
npx wrangler deploy
if ($LASTEXITCODE -ne 0) { Write-Host "  Deploy failed." -ForegroundColor Red; exit 1 }

# -- 3. notification topic as a secret ----------------------------------------
$topic = $null
if (Test-Path ..\config.local.json) {
    try { $topic = (Get-Content ..\config.local.json -Raw | ConvertFrom-Json).ntfy_topic } catch {}
}
if (-not $topic) { $topic = Read-Host "  Enter your ntfy topic (same one your phone subscribes to)" }
$topic | npx wrangler secret put NTFY_TOPIC
Write-Host "  Topic wired: $topic"

# -- 4. hand the ATS tier over from GitHub Actions ----------------------------
# The worker now covers company boards every 1-2 min; tell the Actions run to
# skip them so the same role doesn't alert twice.
$gh = Get-Command gh -ErrorAction SilentlyContinue
if ($gh) {
    try {
        gh variable set SKIP_ATS --body "1"
        Write-Host "  GitHub Actions now runs the aggregator wide-net only."
    } catch {
        Write-Host "  Couldn't set repo variable - run manually: gh variable set SKIP_ATS --body 1" -ForegroundColor Yellow
    }
} else {
    Write-Host "  gh CLI not found - set repo variable SKIP_ATS=1 on GitHub to avoid duplicate alerts." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Done. The instant tier is live - roles hit your phone ~1-3 min after posting." -ForegroundColor Green
Write-Host "First 2 minutes seed silently (no alert flood). Check it: npx wrangler tail"
Write-Host ""
