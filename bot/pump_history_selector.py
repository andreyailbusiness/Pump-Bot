from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .exchange import MexcFuturesClient


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_s(dt: datetime) -> int:
    return int(dt.timestamp())


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            (df["high"] - df["low"]).abs(),
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


@dataclass(frozen=True)
class SymbolScore:
    symbol: str
    score: float
    pump_count: int
    atr_pct: float
    breakout_24h: float
    liquidity_log: float


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    arr = np.array(values, dtype=float)
    lo = float(np.nanmin(arr))
    hi = float(np.nanmax(arr))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return [0.5 for _ in values]
    return [float((x - lo) / (hi - lo)) for x in arr]


def rank_symbols_by_pump_history(
    client: MexcFuturesClient,
    candidate_symbols: list[str],
    ticker_amount24: dict[str, float],
    lookback_days: int = 14,
    min_candles: int = 24 * 5,
) -> list[SymbolScore]:
    """
    Scores symbols by "pump continuation readiness" using:
    - frequency of daily strong moves (pump_count)
    - recent ATR% (volatility)
    - strongest 24h return in lookback (breakout behavior)
    - liquidity proxy (log(amount24))
    """
    end = _utcnow()
    start = end - timedelta(days=lookback_days)
    start_s = _to_s(start)
    end_s = _to_s(end)

    raw: list[dict[str, float | int | str]] = []
    for sym in candidate_symbols:
        try:
            df = client.contract_klines(sym, interval="Min60", start_s=start_s, end_s=end_s)
            if df.empty or df.shape[0] < min_candles:
                continue

            # Pump frequency from daily closes.
            daily_close = df["close"].resample("1D").last().dropna()
            daily_ret = daily_close.pct_change().dropna()
            pump_count = int((daily_ret > 0.08).sum())  # >8% daily move

            tr = _true_range(df)
            atr_pct = float((tr.rolling(24).mean() / df["close"]).dropna().tail(24).mean())
            breakout_24h = float(df["close"].pct_change(24).dropna().max())
            liq = float(ticker_amount24.get(sym, 0.0))
            liquidity_log = float(np.log10(max(liq, 1.0)))

            raw.append(
                {
                    "symbol": sym,
                    "pump_count": pump_count,
                    "atr_pct": atr_pct if np.isfinite(atr_pct) else 0.0,
                    "breakout_24h": breakout_24h if np.isfinite(breakout_24h) else 0.0,
                    "liquidity_log": liquidity_log if np.isfinite(liquidity_log) else 0.0,
                }
            )
        except Exception:
            continue

    if not raw:
        return []

    pump_n = _normalize([float(x["pump_count"]) for x in raw])
    atr_n = _normalize([float(x["atr_pct"]) for x in raw])
    brk_n = _normalize([float(x["breakout_24h"]) for x in raw])
    liq_n = _normalize([float(x["liquidity_log"]) for x in raw])

    # Weighted score: momentum + volatility + liquidity.
    out: list[SymbolScore] = []
    for i, row in enumerate(raw):
        score = 0.35 * pump_n[i] + 0.25 * atr_n[i] + 0.25 * brk_n[i] + 0.15 * liq_n[i]
        out.append(
            SymbolScore(
                symbol=str(row["symbol"]),
                score=float(score),
                pump_count=int(row["pump_count"]),
                atr_pct=float(row["atr_pct"]),
                breakout_24h=float(row["breakout_24h"]),
                liquidity_log=float(row["liquidity_log"]),
            )
        )

    out.sort(key=lambda x: x.score, reverse=True)
    return out

