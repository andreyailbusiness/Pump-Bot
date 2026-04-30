$ErrorActionPreference = "Stop"

param(
  [int]$Days = 30
)

# Candidate profile: 15m + stricter filters + staged exits (half at 3R, remainder trailing; no 1R partial, no hard 5R).
python -m bot.backtester `
  --timeframe 15m `
  --days $Days `
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
  --strict-filters `
  --staged-exits `
  --stage2-r 3 --stage2-close-ratio 0.50 `
  --report-all-months
