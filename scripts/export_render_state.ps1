$ErrorActionPreference = "Stop"

param(
  [string]$BaseUrl = "https://instarding-bot.onrender.com",
  [string]$OutFile = "data\render_state_export.json"
)

$state = Invoke-RestMethod -Uri "$BaseUrl/api/state/export" -Method Get
$json = $state | ConvertTo-Json -Depth 30
$dir = Split-Path -Parent $OutFile
if ($dir -and -not (Test-Path $dir)) {
  New-Item -ItemType Directory -Path $dir | Out-Null
}
$json | Set-Content -Path $OutFile -Encoding UTF8

Write-Host "Export done: $OutFile"
