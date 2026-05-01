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
if ($raw -match "HTTP_CODE:(\d+)$") {
  $code = $Matches[1]
  $body = $raw -replace "`nHTTP_CODE:\d+$", ""
  Write-Host $body
  Write-Host "HTTP $code"
  if ($code -ne "200") { exit 1 }
  $j = $body | ConvertFrom-Json -ErrorAction SilentlyContinue
  if ($j -and $j.merged -eq $false) {
    Write-Warning "Server responded merged=false — deploy may be old; redeploy then retry."
  }
} else {
  Write-Host $raw
}
Write-Host ""
