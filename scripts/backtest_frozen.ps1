$ErrorActionPreference = "Stop"

# Frozen benchmark preset (v1).
# Use this exact command to compare future strategy changes.
python -m bot.backtester `
  --days 90 `
  --limit 120 `
  --fee 0.0004 `
  --slip-bps 2 `
  --selector turnover `
  --universe-source detail `
  --include-majors yes `
  --entry-mode hybrid `
  --mode auto `
  --trend-min-adx 34 `
  --trend-min-move-24h 0.08 `
  --risk-percent-strong 0.015 `
  --risk-percent-weak 0.005 `
  --daily-reselect `
  --daily-top-n 12 `
  --daily-lookback-days 14 `
  --daily-score-quantile 0.75 `
  --report-all-months
