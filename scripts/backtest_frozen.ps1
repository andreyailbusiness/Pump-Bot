$ErrorActionPreference = "Stop"

# Frozen benchmark preset (v2) — «после»: 3-phase regime + kill-switch + weak без impulse.
# Совпадает с логикой рантайма (STRATEGY_ENTRY_MODE_* , WEAK_DISABLE_IMPULSE_ENTRIES, LOSS_STREAK_*).
# Сравнивай будущие изменения только этой же командой.
#
# Ориентир по отчёту (~90 дн, фев–апр): ROI ~49%, MaxDD ~11%, PF ~2, сделок ~120 (числа могут плавать из‑за данных API).

python -m bot.backtester `
  --timeframe 1h `
  --days 90 `
  --limit 120 `
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
  --report-all-months

# Candidate profile for comparison:
# powershell -NoProfile -ExecutionPolicy Bypass -File "scripts/backtest_candidate_15m.ps1" -Days 90
