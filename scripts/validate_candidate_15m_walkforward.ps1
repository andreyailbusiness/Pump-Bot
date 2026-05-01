param(
  [double]$Fee = 0.0007,
  [double]$SlipBps = 6,
  [int]$WarmupDays = 45
)

$ErrorActionPreference = "Stop"

# Walk-forward OOS validation for the current 15m candidate.
# Runs fixed quarter-like windows with identical strategy config.
$windows = @(
  @{ Since = "2025-05-01"; Until = "2025-08-31" },
  @{ Since = "2025-09-01"; Until = "2025-12-31" },
  @{ Since = "2026-01-01"; Until = "2026-04-30" }
)

foreach ($w in $windows) {
  Write-Host ""
  Write-Host "=== Walk-forward window: $($w.Since) .. $($w.Until) | fee=$Fee | slip=$SlipBps bps ===" -ForegroundColor Cyan
  python -m bot.backtester `
    --timeframe 15m `
    --since $w.Since `
    --until $w.Until `
    --warmup-days $WarmupDays `
    --fee $Fee `
    --slip-bps $SlipBps `
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
    --stage2-r 3 `
    --stage2-close-ratio 0.50 `
    --report-all-months
}

