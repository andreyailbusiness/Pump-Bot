$ErrorActionPreference = "Stop"

# April 1 -> now (UTC), same logic as frozen v2. Edit year in --since if needed.
# Пример: весь апрель 2026 с 1-го по сегодня.

python -m bot.backtester `
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
  --limit 120 `
  --report-all-months
