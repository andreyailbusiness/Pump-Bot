$ErrorActionPreference = "Stop"

param(
  [string]$BaseUrl = "https://instarding-bot.onrender.com",
  [string]$StateFile = "data\render_state_export.json"
)

if (-not (Test-Path $StateFile)) {
  throw "State file not found: $StateFile"
}

$body = Get-Content -Path $StateFile -Raw
Invoke-RestMethod -Uri "$BaseUrl/api/state/import" -Method Post -ContentType "application/json" -Body $body | Out-Null

Write-Host "Restore done from: $StateFile"
