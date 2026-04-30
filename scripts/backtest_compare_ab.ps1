$ErrorActionPreference = "Stop"

Write-Host "=== Baseline 1h, last 30 days ==="
python -m bot.backtester `
  --timeframe 1h `
  --days 30 `
  --fee 0.0004 `
  --slip-bps 2 `
  --selector pump `
  --universe-source detail `
  --include-majors yes `
  --mode auto `
  --trend-min-adx 34 `
  --trend-min-move-24h 0.08 `
  --min-breadth-count 6 `
  --neutral-min-breadth-count 3 `
  --entry-mode-strong pump `
  --entry-mode-neutral hybrid `
  --entry-mode-weak hybrid `
  --risk-percent-strong 0.015 `
  --risk-percent-neutral 0.01 `
  --risk-percent-weak 0.005 `
  --weak-disable-impulse-entries `
  --loss-streak-block-threshold 2 `
  --loss-streak-block-hours 72 `
  --daily-reselect `
  --daily-top-n 0 `
  --daily-lookback-days 14 `
  --daily-score-quantile 0.75 `
  --limit 120

Write-Host "`n=== Candidate 15m, last 30 days ==="
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts/backtest_candidate_15m.ps1" -Days 30

Write-Host "`n=== Baseline 1h, April MTD ==="
python -m bot.backtester `
  --timeframe 1h `
  --since 2026-04-01 `
  --fee 0.0004 `
  --slip-bps 2 `
  --selector pump `
  --universe-source detail `
  --include-majors yes `
  --mode auto `
  --trend-min-adx 34 `
  --trend-min-move-24h 0.08 `
  --min-breadth-count 6 `
  --neutral-min-breadth-count 3 `
  --entry-mode-strong pump `
  --entry-mode-neutral hybrid `
  --entry-mode-weak hybrid `
  --risk-percent-strong 0.015 `
  --risk-percent-neutral 0.01 `
  --risk-percent-weak 0.005 `
  --weak-disable-impulse-entries `
  --loss-streak-block-threshold 2 `
  --loss-streak-block-hours 72 `
  --daily-reselect `
  --daily-top-n 0 `
  --daily-lookback-days 14 `
  --daily-score-quantile 0.75 `
  --limit 120

Write-Host "`n=== Candidate 15m, last 90 days ==="
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts/backtest_candidate_15m.ps1" -Days 90
