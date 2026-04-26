from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

import pandas as pd

from .indicators import adx, atr, bollinger_bands, ema


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class StrategyParams:
    ema_period: int = 200
    adx_period: int = 14
    adx_threshold: float = 25.0
    atr_period: int = 14
    atr_min_pct: float = 0.005
    boll_period: int = 20
    boll_std: float = 2.0
    pullback_lookback: int = 10
    entry_mode: Literal["retest", "momentum", "hybrid", "pump"] = "hybrid"
    pump_lookback: int = 6
    pump_min_ret_1h: float = 0.012
    pump_volume_mult: float = 1.4
    pump_close_pos_min: float = 0.55
    pump_max_overext_atr: float = 2.2
    pump_max_opp_wick_ratio: float = 2.0
    pump_short_min_ret_1h: float = 0.018
    pump_short_volume_mult: float = 1.8
    pump_short_close_pos_min: float = 0.65
    pump_short_max_overext_atr: float = 1.4
    pump_short_max_opp_wick_ratio: float = 1.0


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    entry_price: float
    atr: float
    timestamp: pd.Timestamp
    reason: str


def _pinbar_like(row: pd.Series, bullish: bool) -> bool:
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    # "Pin-bar" as a bias confirmation: dominant wick against direction.
    # For long: big lower wick; for short: big upper wick.
    if bullish:
        return (c > o) and (lower_wick > max(2 * body, 1e-12)) and (upper_wick <= lower_wick)
    return (c < o) and (upper_wick > max(2 * body, 1e-12)) and (lower_wick <= upper_wick)


def generate_signal(symbol: str, df: pd.DataFrame, p: StrategyParams) -> Signal | None:
    """
    df: indexed by datetime, columns: open/high/low/close/volume at minimum.
    Uses last closed candle for decisions.
    """
    if df.shape[0] < max(p.ema_period, p.boll_period) + p.pullback_lookback + 5:
        return None

    close = df["close"]
    df = df.copy()

    df["ema200"] = ema(close, p.ema_period)
    df["adx"] = adx(df, p.adx_period)
    df["atr"] = atr(df, p.atr_period)
    lower, mid, upper = bollinger_bands(close, p.boll_period, p.boll_std)
    df["bb_lower"] = lower
    df["bb_mid"] = mid
    df["bb_upper"] = upper
    df["vol_ma20"] = df["volume"].rolling(20).mean()

    last = df.iloc[-2]  # last closed candle (assuming newest is still forming)
    last_i = df.index[-2]
    price = float(last["close"])

    ema200 = float(last["ema200"])
    if pd.isna(ema200):
        return None

    trend_side: Side = Side.LONG if price > ema200 else Side.SHORT

    last_adx = float(last["adx"]) if not pd.isna(last["adx"]) else None
    if last_adx is None or last_adx < p.adx_threshold:
        return None

    last_atr = float(last["atr"]) if not pd.isna(last["atr"]) else None
    if last_atr is None or last_atr <= 0:
        return None
    if (last_atr / price) < p.atr_min_pct:
        return None

    # Breakout detection within lookback window.
    window = df.iloc[-(p.pullback_lookback + 2) : -1]  # up to last closed candle
    if window.shape[0] < 5:
        return None

    breakout_idx = None
    if trend_side == Side.LONG:
        breakout_mask = window["close"] > window["bb_upper"]
    else:
        breakout_mask = window["close"] < window["bb_lower"]
    if breakout_mask.any():
        breakout_idx = breakout_mask[breakout_mask].index[-1]
    if breakout_idx is None:
        return None

    # Pullback: after breakout, price returns to SMA20 (bb_mid) and prints a confirmation candle.
    after_breakout = window.loc[breakout_idx:]
    if after_breakout.shape[0] < 2:
        return None

    # Path A: breakout + retest (existing logic)
    retest_ok = False
    retest_reason = ""
    touch_mid = (after_breakout["low"] <= after_breakout["bb_mid"]) & (after_breakout["high"] >= after_breakout["bb_mid"])
    if touch_mid.any():
        first_touch_idx = touch_mid[touch_mid].index[0]
        if last_i >= first_touch_idx:
            if trend_side == Side.LONG:
                confirm_ok = _pinbar_like(last, bullish=True) or (float(last["close"]) > float(last["open"]))
                if confirm_ok:
                    retest_ok = True
                    retest_reason = "EMA>200, ADX strong, ATR ok, breakout upper BB, pullback to SMA20"
            else:
                confirm_ok = _pinbar_like(last, bullish=False) or (float(last["close"]) < float(last["open"]))
                if confirm_ok:
                    retest_ok = True
                    retest_reason = "EMA<200, ADX strong, ATR ok, breakout lower BB, pullback to SMA20"

    # Path B: momentum continuation (no mandatory retest)
    momentum_ok = False
    momentum_reason = ""
    prev = df.iloc[-3]
    if trend_side == Side.LONG:
        momentum_ok = (float(last["close"]) > float(last["bb_upper"])) and (float(last["close"]) > float(prev["high"]))
        momentum_reason = "EMA>200, ADX strong, ATR ok, momentum continuation above BB"
    else:
        momentum_ok = (float(last["close"]) < float(last["bb_lower"])) and (float(last["close"]) < float(prev["low"]))
        momentum_reason = "EMA<200, ADX strong, ATR ok, momentum continuation below BB"

    # Path C: pump impulse scanner (fresh breakout in recent candles).
    pump_ok = False
    pump_reason = ""
    if "volume" in df.columns and df.shape[0] >= 4:
        c_last = float(last["close"])
        overext = abs(c_last - float(mid.loc[last_i])) / max(last_atr, 1e-12) if last_i in mid.index else 0.0
        not_exhausted_long = overext <= p.pump_max_overext_atr
        not_exhausted_short = overext <= p.pump_short_max_overext_atr
        recent = df.iloc[-(max(3, p.pump_lookback) + 2) : -1]

        if trend_side == Side.LONG:
            fresh_long = False
            for j in range(1, recent.shape[0]):
                row = recent.iloc[j]
                prev_row = recent.iloc[j - 1]
                c = float(row["close"])
                o = float(row["open"])
                h = float(row["high"])
                l = float(row["low"])
                bb_u = float(row["bb_upper"]) if not pd.isna(row["bb_upper"]) else None
                prev_bb_u = float(prev_row["bb_upper"]) if not pd.isna(prev_row["bb_upper"]) else None
                prev_close = float(prev_row["close"])
                v = float(row["volume"]) if not pd.isna(row["volume"]) else 0.0
                vma = float(row["vol_ma20"]) if not pd.isna(row["vol_ma20"]) else 0.0
                rng = max(h - l, 1e-12)
                close_pos = (c - l) / rng
                ret = (c / o - 1.0) if o > 0 else 0.0
                body = abs(c - o)
                upper_wick = h - max(c, o)
                vol_ok = vma > 0 and (v >= p.pump_volume_mult * vma)
                impulse_ok = (ret >= p.pump_min_ret_1h) and (close_pos >= p.pump_close_pos_min)
                breakout_ok = (
                    (bb_u is not None)
                    and (prev_bb_u is not None)
                    and (c > bb_u)
                    and (prev_close <= prev_bb_u)
                )
                wick_ok = upper_wick <= p.pump_max_opp_wick_ratio * max(body, 1e-12)
                # Long side is intentionally softer: we keep fresh breakout+impulse and
                # avoid over-constraining continuation to prevent missing early pumps.
                if impulse_ok and vol_ok and breakout_ok and wick_ok:
                    fresh_long = True
                    break
            # Enter only if fresh breakout happened recently and price did not run too far yet.
            if fresh_long and not_exhausted_long:
                pump_ok = True
                pump_reason = "Recent fresh long pump breakout (impulse+volume), avoid late chase"
        else:
            fresh_short = False
            for j in range(1, recent.shape[0]):
                row = recent.iloc[j]
                prev_row = recent.iloc[j - 1]
                c = float(row["close"])
                o = float(row["open"])
                h = float(row["high"])
                l = float(row["low"])
                bb_l = float(row["bb_lower"]) if not pd.isna(row["bb_lower"]) else None
                prev_bb_l = float(prev_row["bb_lower"]) if not pd.isna(prev_row["bb_lower"]) else None
                prev_close = float(prev_row["close"])
                v = float(row["volume"]) if not pd.isna(row["volume"]) else 0.0
                vma = float(row["vol_ma20"]) if not pd.isna(row["vol_ma20"]) else 0.0
                rng = max(h - l, 1e-12)
                close_pos_short = (h - c) / rng
                ret = (c / o - 1.0) if o > 0 else 0.0
                body = abs(c - o)
                lower_wick = min(c, o) - l
                vol_ok = vma > 0 and (v >= p.pump_short_volume_mult * vma)
                impulse_ok = (ret <= -p.pump_short_min_ret_1h) and (close_pos_short >= p.pump_short_close_pos_min)
                breakout_ok = (
                    (bb_l is not None)
                    and (prev_bb_l is not None)
                    and (c < bb_l)
                    and (prev_close >= prev_bb_l)
                )
                wick_ok = lower_wick <= p.pump_short_max_opp_wick_ratio * max(body, 1e-12)
                continuation_ok = c <= float(last["bb_lower"]) if not pd.isna(last["bb_lower"]) else True
                if impulse_ok and vol_ok and breakout_ok and wick_ok and continuation_ok:
                    fresh_short = True
                    break
            if fresh_short and not_exhausted_short:
                pump_ok = True
                pump_reason = "Recent fresh short dump breakout (impulse+volume), avoid late chase"

    use_retest = p.entry_mode in ("retest", "hybrid") and retest_ok
    use_momentum = p.entry_mode in ("momentum", "hybrid") and momentum_ok
    use_pump = p.entry_mode == "pump" and pump_ok
    if not (use_retest or use_momentum or use_pump):
        return None
    reason = retest_reason if use_retest else (momentum_reason if use_momentum else pump_reason)

    return Signal(
        symbol=symbol,
        side=trend_side,
        entry_price=price,
        atr=last_atr,
        timestamp=last_i,
        reason=reason,
    )

