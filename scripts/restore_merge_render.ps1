# Merge-import snapshot into running bot (does not replace all state).
# On Render, disk is ephemeral: enable GITHUB_STATE_SYNC_* so restarts keep this data,
# or re-run this script after each deploy/restart.
param(
  [string]$BaseUrl = "https://instarding-bot.onrender.com",
  [switch]$ApplyEquity
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$json = Join-Path $here "restore_state_2026-04-30.json"

$q = "merge=true"
if ($ApplyEquity) { $q += "&apply_equity=true" }

$url = "$BaseUrl/api/state/import?$q"
Write-Host "POST $url"
$raw = curl.exe -s -S -w "`nHTTP_CODE:%{http_code}" -X POST $url -H "Content-Type: application/json" --data-binary "@$json"
$split = $raw -split "HTTP_CODE:", 2
$body = $split[0].Trim()
if ($split.Count -ge 2) {
  $code = $split[1].Trim() -replace "`r", ""
  Write-Host $body
  Write-Host "HTTP $code"
  if ($code -ne "200") { exit 1 }
  $j = $body | ConvertFrom-Json -ErrorAction SilentlyContinue
  if ($j -and $j.merged -eq $false) {
    Write-Warning "Server responded merged=false; deploy may be old - redeploy then retry."
  }
} else {
  Write-Host $raw
  exit 1
}
Write-Host ""
