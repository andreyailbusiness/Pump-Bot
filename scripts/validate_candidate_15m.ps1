param(
  [string]$Since = "2026-01-01",
  [string]$Until = "2026-04-30",
  [int]$WarmupDays = 45
)

$ErrorActionPreference = "Stop"

# Validation preset for current best candidate (15m):
# - keeps the exact candidate logic/config
# - runs baseline cost and stressed-cost scenarios on the same holdout window

$baseArgs = @(
  "-m", "bot.backtester",
  "--timeframe", "15m",
  "--since", $Since,
  "--until", $Until,
  "--warmup-days", "$WarmupDays",
  "--fee", "0.0004",
  "--slip-bps", "2",
  "--selector", "pump",
  "--universe-source", "detail",
  "--include-majors", "yes",
  "--mode", "auto",
  "--trend-min-adx", "34",
  "--trend-min-move-24h", "0.08",
  "--min-breadth-count", "6",
  "--neutral-min-breadth-count", "3",
  "--entry-mode-strong", "pump",
  "--entry-mode-neutral", "hybrid",
  "--entry-mode-weak", "hybrid",
  "--risk-percent-strong", "0.015",
  "--risk-percent-neutral", "0.01",
  "--risk-percent-weak", "0.005",
  "--weak-disable-impulse-entries",
  "--loss-streak-block-threshold", "2",
  "--loss-streak-block-hours", "72",
  "--daily-reselect",
  "--daily-top-n", "0",
  "--daily-lookback-days", "14",
  "--daily-score-quantile", "0.75",
  "--limit", "120",
  "--strict-filters",
  "--staged-exits",
  "--stage2-r", "3",
  "--stage2-close-ratio", "0.50",
  "--report-all-months"
)

Write-Host "=== Validation A: candidate costs (fee=0.0004, slip=2bps) ===" -ForegroundColor Cyan
python @baseArgs

Write-Host "`n=== Validation B: stress costs (fee=0.0007, slip=6bps) ===" -ForegroundColor Yellow
$stressArgs = $baseArgs.Clone()
for ($i = 0; $i -lt $stressArgs.Count; $i++) {
  if ($stressArgs[$i] -eq "--fee") { $stressArgs[$i + 1] = "0.0007" }
  if ($stressArgs[$i] -eq "--slip-bps") { $stressArgs[$i + 1] = "6" }
}
python @stressArgs

