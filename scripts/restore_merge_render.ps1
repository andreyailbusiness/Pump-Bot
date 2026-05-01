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
curl.exe -s -X POST $url -H "Content-Type: application/json" --data-binary "@$json"
Write-Host ""
