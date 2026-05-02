"""
Dynamic score used by backtester daily-reselect and live universe filtering (parity).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def bars_per_day_for_timeframe(timeframe: str) -> int:
    if timeframe == "15m":
        return 96
    return 24


def compute_dynamic_score(sub: pd.DataFrame, bars_per_day: int = 24) -> float:
    """
    Same formula as historical portfolio backtest: pump-day count, ATR%, breakout strength.
    Returns -1e9 if insufficient history.
    """
    if sub.shape[0] < max(120, bars_per_day * 5):
        return -1e9
    daily_close = sub["close"].resample("1D").last().dropna()
    daily_ret = daily_close.pct_change().dropna()
    pump_count = float((daily_ret > 0.08).sum())
    prev_close = sub["close"].shift(1)
    tr = pd.concat(
        [
            (sub["high"] - sub["low"]).abs(),
            (sub["high"] - prev_close).abs(),
            (sub["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_pct = float((tr.rolling(bars_per_day).mean() / sub["close"]).dropna().tail(bars_per_day).mean() or 0.0)
    brk = float(sub["close"].pct_change(bars_per_day).dropna().tail(bars_per_day * 3).max() or 0.0)
    return 2.0 * pump_count + 80.0 * atr_pct + 10.0 * brk


def select_symbols_by_dynamic_score(
    scored: list[tuple[str, float]],
    score_quantile: float,
    top_n: int,
) -> list[str]:
    """
    Match backtest_portfolio_daily_reselect: sort by score desc, keep scores >= quantile cutoff, optional top_n cap.
    """
    valid = [(s, sc) for s, sc in scored if sc > -1e8]
    if not valid:
        return []
    valid.sort(key=lambda x: x[1], reverse=True)
    scores = np.array([x[1] for x in valid], dtype=float)
    q = float(np.clip(score_quantile, 0.0, 1.0))
    cutoff = float(np.quantile(scores, q))
    selected = [(s, sc) for s, sc in valid if sc >= cutoff]
    if top_n > 0:
        selected = selected[:top_n]
    return [s for s, _ in selected]
