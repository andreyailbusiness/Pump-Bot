from __future__ import annotations

import argparse
import math
import os
import pickle
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .config import get_settings
from .exchange import MexcFuturesClient
from .indicators import adx as adx_fn, atr as atr_fn
from .pump_history_selector import rank_symbols_by_pump_history
from .risk_manager import RiskParams, build_order_plan, tranche_risk_percent
from .strategy import StrategyParams, generate_signal
from .symbols import (
    TopSymbolsCache,
    get_futures_candidates_with_turnover,
    get_futures_contracts_from_detail,
    get_top_futures_contracts_by_turnover,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_day_utc(s: str) -> datetime:
    """Parse YYYY-MM-DD as UTC midnight."""
    return datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _clip_symbol_dfs_history(
    symbol_dfs: dict[str, pd.DataFrame],
    history_start: datetime | None,
    history_end: datetime | None,
    min_rows: int = 200,
) -> dict[str, pd.DataFrame]:
    """Restrict rows to [history_start, history_end] for each symbol (warm-up safe)."""
    out: dict[str, pd.DataFrame] = {}
    hs = pd.Timestamp(history_start) if history_start is not None else None
    he = pd.Timestamp(history_end) if history_end is not None else None
    for sym, df in symbol_dfs.items():
        if df is None or df.empty:
            continue
        d = df
        if hs is not None:
            d = d[d.index >= hs]
        if he is not None:
            d = d[d.index <= he]
        if int(d.shape[0]) >= min_rows:
            out[sym] = d
    return out


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def _s(dt: datetime) -> int:
    return int(dt.timestamp())


def _timeframe_to_mexc_interval(timeframe: str) -> str:
    if timeframe == "15m":
        return "Min15"
    return "Min60"


def _bars_per_day(timeframe: str) -> int:
    if timeframe == "15m":
        return 96
    return 24


def fetch_futures_history(client: MexcFuturesClient, symbol: str, days: int, timeframe: str = "1h") -> pd.DataFrame:
    """
    Futures: fetches up to `days` of candles by paging contract klines (max 2000 points per call).

    MEXC returns at most 2000 bars per request; for long ranges we page **backward** from `end`
    (same pattern as many exchanges: each window ends just before the previous chunk's first bar).
    """
    end = _utcnow()
    start = end - timedelta(days=days)
    start_s = _s(start)
    end_s = _s(end)

    out: list[pd.DataFrame] = []
    chunk_end = end_s
    safety = 0

    while chunk_end > start_s and safety < 1000:
        safety += 1
        df = client.contract_klines(
            symbol, interval=_timeframe_to_mexc_interval(timeframe), start_s=start_s, end_s=chunk_end
        )
        if df.empty:
            break
        out.append(df)
        first_open = int(df["open_time_s"].iloc[0])
        if first_open <= start_s:
            break
        chunk_end = first_open - 1
        if df.shape[0] < 2000:
            break

    if not out:
        return pd.DataFrame()

    full = pd.concat(out).sort_index()
    full = full[~full.index.duplicated(keep="last")]
    ts0 = pd.Timestamp(start)
    if ts0.tzinfo is None:
        ts0 = ts0.tz_localize("UTC")
    else:
        ts0 = ts0.tz_convert("UTC")
    full = full[full.index >= ts0]
    return full


def fetch_funding_rates(client: MexcFuturesClient, symbol: str, start_ms: int, end_ms: int) -> dict[int, float]:
    """
    Returns mapping settleTime(ms) -> fundingRate for [start_ms, end_ms].
    Pages until we covered the range or data ends.
    """
    page = 1
    out: dict[int, float] = {}
    while page < 50:
        rows = client.funding_rate_history(symbol, page_num=page, page_size=1000)
        if not rows:
            break
        for r in rows:
            try:
                st = int(r.get("settleTime"))
                rate = float(r.get("fundingRate"))
            except Exception:
                continue
            if st < start_ms or st > end_ms:
                continue
            out[st] = rate
        # If this page has records all earlier than start, we can stop.
        min_settle = None
        try:
            min_settle = min(int(x.get("settleTime")) for x in rows if x.get("settleTime") is not None)
        except Exception:
            min_settle = None
        if min_settle is not None and min_settle < start_ms:
            break
        page += 1
    return out


def _cache_dir() -> str:
    d = os.path.join("data", "backtest_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _futures_cache_fname(symbol: str, days: int, timeframe: str) -> str:
    tf_tag = timeframe.replace("/", "_")
    # v2: multi-page klines fetch (older caches were truncated to one 2000-bar page).
    return os.path.join(_cache_dir(), f"futures_{tf_tag}_{symbol}_{days}d_v2.pkl")


def load_cached_df(symbol: str, days: int, timeframe: str = "1h") -> pd.DataFrame | None:
    path = _futures_cache_fname(symbol, days, timeframe)
    try:
        return pd.read_pickle(path)
    except Exception:
        return None


def save_cached_df(symbol: str, days: int, df: pd.DataFrame, timeframe: str = "1h") -> None:
    path = _futures_cache_fname(symbol, days, timeframe)
    try:
        df.to_pickle(path)
    except Exception:
        return


def load_cached_funding(symbol: str, days: int) -> dict[int, float] | None:
    path = os.path.join(_cache_dir(), f"funding_{symbol}_{days}d.pkl")
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def save_cached_funding(symbol: str, days: int, m: dict[int, float]) -> None:
    path = os.path.join(_cache_dir(), f"funding_{symbol}_{days}d.pkl")
    try:
        with open(path, "wb") as f:
            pickle.dump(m, f)
    except Exception:
        return


@dataclass
class BtResult:
    symbol: str
    trades: int
    wins: int
    losses: int
    roi: float
    max_dd: float
    profit_factor: float
    avg_trade_pnl: float
    sharpe_trades: float
    trades_per_month: float
    monthly_pnl: dict[str, float]


@dataclass
class PortfolioResult:
    start_equity: float
    end_equity: float
    trades: int
    wins: int
    losses: int
    roi: float
    max_dd: float
    profit_factor: float
    avg_trade_pnl: float
    sharpe_trades: float
    trades_per_month: float
    monthly_pnl: dict[str, float]
    symbol_pnl: dict[str, float]
    symbol_monthly_pnl: dict[str, dict[str, float]]


def _apply_slippage(price: float, side: str, slip_bps: float, is_entry: bool) -> float:
    slip = (slip_bps / 10000.0) if slip_bps else 0.0
    if side == "long":
        return price * (1 + slip) if is_entry else price * (1 - slip)
    return price * (1 - slip) if is_entry else price * (1 + slip)


def _fee(notional: float, fee_rate: float) -> float:
    return abs(notional) * fee_rate


def backtest_symbol(
    symbol: str,
    df: pd.DataFrame,
    strat: StrategyParams,
    risk: RiskParams,
    taker_fee_rate: float,
    slippage_bps: float,
    funding_by_settle_ms: dict[int, float] | None = None,
    reverse_signals: bool = False,
    reverse_max_adx: float = 35.0,
    reverse_max_move_24h: float = 0.08,
    partial_tp_ratio: float = 0.5,
    mode: str = "auto",
    trend_min_adx: float = 30.0,
    trend_min_move_24h: float = 0.06,
    risk_percent_strong: float | None = None,
    risk_percent_weak: float | None = None,
    entry_mode_strong: str | None = None,
    entry_mode_weak: str | None = None,
    bars_per_day: int = 24,
) -> BtResult:
    equity = 1000.0
    peak = equity
    max_dd = 0.0

    pos = None  # dict with fields includes qty_open, tp1_done
    trades = 0
    wins = 0
    losses = 0
    trade_pnls: list[float] = []
    trade_months: list[str] = []
    funding_pnl_total: float = 0.0
    applied_funding: set[int] = set()
    pyramids_done = 0
    tranche_risk = tranche_risk_percent(risk.risk_percent, risk.pyramids_max)

    monthly_pnl: dict[str, float] = {}
    warmup_bars = max(250, bars_per_day * 10)
    if df.shape[0] < warmup_bars + 2:
        return BtResult(symbol, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, {})

    atr_series = atr_fn(df, strat.atr_period)
    adx_series = adx_fn(df, strat.adx_period)
    move_24h = df["close"].pct_change(bars_per_day).abs()

    # Walk forward; generate_signal expects "last candle forming", so we pass slices and treat i as "now".
    for i in range(warmup_bars, len(df) - 1):
        window = df.iloc[: i + 1]
        last_closed = window.iloc[-2]
        last_close = float(last_closed["close"])
        last_high = float(last_closed["high"])
        last_low = float(last_closed["low"])
        # Candle open time in ms (futures data provides seconds)
        candle_open_ms = int(last_closed.get("open_time_s", 0)) * 1000
        atr_val = float(atr_series.iloc[i - 1]) if not pd.isna(atr_series.iloc[i - 1]) else 0.0
        adx_val = float(adx_series.iloc[i - 1]) if not pd.isna(adx_series.iloc[i - 1]) else 0.0
        move24 = float(move_24h.iloc[i - 1]) if not pd.isna(move_24h.iloc[i - 1]) else 0.0

        if pos:
            # Apply funding if any settleTimes happened up to this candle.
            if funding_by_settle_ms:
                for settle_ms, rate in funding_by_settle_ms.items():
                    if settle_ms in applied_funding:
                        continue
                    if settle_ms <= candle_open_ms:
                        # Approximate funding notional by close price at this time.
                        notional = pos["qty"] * last_close
                        # Convention: longs pay when rate>0; shorts receive when rate>0.
                        funding = -notional * rate if pos["side"] == "long" else notional * rate
                        equity += funding
                        funding_pnl_total += funding
                        mkey = str(pd.to_datetime(settle_ms, unit="ms", utc=True).tz_convert(None).to_period("M"))
                        monthly_pnl[mkey] = monthly_pnl.get(mkey, 0.0) + float(funding)
                        applied_funding.add(settle_ms)

            # Trailing logic (close-based activation) + intrabar hit checks on high/low (conservative order).
            if atr_val > 0:
                if pos["side"] == "long":
                    profit = last_close - pos["entry"]
                    if (not pos["trailing_active"]) and profit >= risk.trail_activate_atr_mult * atr_val:
                        pos["trailing_active"] = True
                        pos["trailing_sl"] = max(pos["sl"], last_close - risk.trail_dist_atr_mult * atr_val)
                    elif pos["trailing_active"] and pos["trailing_sl"] is not None:
                        pos["trailing_sl"] = max(pos["trailing_sl"], last_close - risk.trail_dist_atr_mult * atr_val)
                else:
                    profit = pos["entry"] - last_close
                    if (not pos["trailing_active"]) and profit >= risk.trail_activate_atr_mult * atr_val:
                        pos["trailing_active"] = True
                        pos["trailing_sl"] = min(pos["sl"], last_close + risk.trail_dist_atr_mult * atr_val)
                    elif pos["trailing_active"] and pos["trailing_sl"] is not None:
                        pos["trailing_sl"] = min(pos["trailing_sl"], last_close + risk.trail_dist_atr_mult * atr_val)

            # Pyramiding: add-on each +N*ATR move in favor, capped by pyramids_max.
            if atr_val > 0 and pyramids_done < int(risk.pyramids_max):
                trigger_move = (pyramids_done + 1) * risk.pyramid_trigger_atr_mult * atr_val
                if pos["side"] == "long":
                    moved = last_close - pos["entry"]
                    if moved >= trigger_move:
                        # add tranche
                        sig_like = type("Sig", (), {"side": type("S", (), {"value": "long"})(), "entry_price": last_close, "atr": atr_val})
                        rp = RiskParams(
                            risk_percent=tranche_risk,
                            leverage=risk.leverage,
                            rr=risk.rr,
                            sl_atr_mult=risk.sl_atr_mult,
                            trail_activate_atr_mult=risk.trail_activate_atr_mult,
                            trail_dist_atr_mult=risk.trail_dist_atr_mult,
                            pyramids_max=risk.pyramids_max,
                            pyramid_trigger_atr_mult=risk.pyramid_trigger_atr_mult,
                        )
                        plan = build_order_plan(equity=equity, signal=sig_like, p=rp)
                        if plan and plan.qty > 0:
                            add_entry_exec = _apply_slippage(plan.entry, "long", slippage_bps, is_entry=True)
                            fee_entry = _fee(plan.qty * add_entry_exec, taker_fee_rate)
                            equity -= fee_entry
                            # Weighted average entry_exec & qty
                            new_qty = pos["qty"] + plan.qty
                            pos["entry_exec"] = (pos["entry_exec"] * pos["qty"] + add_entry_exec * plan.qty) / new_qty
                            pos["qty"] = new_qty
                            pyramids_done += 1
                else:
                    moved = pos["entry"] - last_close
                    if moved >= trigger_move:
                        sig_like = type("Sig", (), {"side": type("S", (), {"value": "short"})(), "entry_price": last_close, "atr": atr_val})
                        rp = RiskParams(
                            risk_percent=tranche_risk,
                            leverage=risk.leverage,
                            rr=risk.rr,
                            sl_atr_mult=risk.sl_atr_mult,
                            trail_activate_atr_mult=risk.trail_activate_atr_mult,
                            trail_dist_atr_mult=risk.trail_dist_atr_mult,
                            pyramids_max=risk.pyramids_max,
                            pyramid_trigger_atr_mult=risk.pyramid_trigger_atr_mult,
                        )
                        plan = build_order_plan(equity=equity, signal=sig_like, p=rp)
                        if plan and plan.qty > 0:
                            add_entry_exec = _apply_slippage(plan.entry, "short", slippage_bps, is_entry=True)
                            fee_entry = _fee(plan.qty * add_entry_exec, taker_fee_rate)
                            equity -= fee_entry
                            new_qty = pos["qty"] + plan.qty
                            pos["entry_exec"] = (pos["entry_exec"] * pos["qty"] + add_entry_exec * plan.qty) / new_qty
                            pos["qty"] = new_qty
                            pyramids_done += 1

            sl_level = pos["trailing_sl"] if pos["trailing_active"] and pos["trailing_sl"] is not None else pos["sl"]
            if pos.get("tp1_done"):
                # Protect remainder at breakeven once 1st TP is taken.
                if pos["side"] == "long":
                    sl_level = max(sl_level, pos["entry_exec"])
                else:
                    sl_level = min(sl_level, pos["entry_exec"])

            hit = None
            exit_price = None
            if pos["side"] == "long":
                sl_hit = last_low <= sl_level
                tp_hit = last_high >= pos["tp"]
                # Partial TP first.
                if tp_hit and (not pos.get("tp1_done", False)):
                    close_qty = pos["qty_open"] * partial_tp_ratio
                    if close_qty > 0:
                        exec_tp = _apply_slippage(pos["tp"], pos["side"], slippage_bps, is_entry=False)
                        fee_tp = _fee(close_qty * exec_tp, taker_fee_rate)
                        pnl_tp = (exec_tp - pos["entry_exec"]) * close_qty
                        net_tp = pnl_tp - fee_tp
                        equity += net_tp
                        pos["qty_open"] -= close_qty
                        trade_pnls.append(net_tp)
                        ts = window.index[-2]
                        if getattr(ts, "tzinfo", None) is not None:
                            ts = ts.tz_convert(None)
                        m = str(ts.to_period("M"))
                        trade_months.append(m)
                        monthly_pnl[m] = monthly_pnl.get(m, 0.0) + float(net_tp)
                        pos["tp1_done"] = True
                        # Start trailing remainder after first TP.
                        pos["trailing_active"] = True
                if sl_hit and tp_hit:
                    hit = "sl"  # conservative
                    exit_price = sl_level
                elif sl_hit:
                    hit = "sl"
                    exit_price = sl_level
                elif tp_hit:
                    hit = "tp"
                    exit_price = pos["tp"]
            else:
                sl_hit = last_high >= sl_level
                tp_hit = last_low <= pos["tp"]
                if tp_hit and (not pos.get("tp1_done", False)):
                    close_qty = pos["qty_open"] * partial_tp_ratio
                    if close_qty > 0:
                        exec_tp = _apply_slippage(pos["tp"], pos["side"], slippage_bps, is_entry=False)
                        fee_tp = _fee(close_qty * exec_tp, taker_fee_rate)
                        pnl_tp = (pos["entry_exec"] - exec_tp) * close_qty
                        net_tp = pnl_tp - fee_tp
                        equity += net_tp
                        pos["qty_open"] -= close_qty
                        trade_pnls.append(net_tp)
                        ts = window.index[-2]
                        if getattr(ts, "tzinfo", None) is not None:
                            ts = ts.tz_convert(None)
                        m = str(ts.to_period("M"))
                        trade_months.append(m)
                        monthly_pnl[m] = monthly_pnl.get(m, 0.0) + float(net_tp)
                        pos["tp1_done"] = True
                        pos["trailing_active"] = True
                if sl_hit and tp_hit:
                    hit = "sl"
                    exit_price = sl_level
                elif sl_hit:
                    hit = "sl"
                    exit_price = sl_level
                elif tp_hit:
                    hit = "tp"
                    exit_price = pos["tp"]

            if hit:
                # Apply slippage and taker fee on exit.
                raw_exit = float(exit_price) if exit_price is not None else last_close
                exec_exit = _apply_slippage(raw_exit, pos["side"], slippage_bps, is_entry=False)
                qty_close = pos["qty_open"]
                notional_exit = qty_close * exec_exit
                fee_exit = _fee(notional_exit, taker_fee_rate)

                pnl = (exec_exit - pos["entry_exec"]) * qty_close if pos["side"] == "long" else (pos["entry_exec"] - exec_exit) * qty_close
                net = pnl - fee_exit
                equity += net
                trades += 1
                trade_pnls.append(net)
                ts = window.index[-2]
                if getattr(ts, "tzinfo", None) is not None:
                    ts = ts.tz_convert(None)
                trade_months.append(str(ts.to_period("M")))
                monthly_pnl[trade_months[-1]] = monthly_pnl.get(trade_months[-1], 0.0) + float(net)
                if net >= 0:
                    wins += 1
                else:
                    losses += 1
                pos = None
                pyramids_done = 0

                peak = max(peak, equity)
                dd = (peak - equity) / peak if peak > 0 else 0
                max_dd = max(max_dd, dd)
                continue

        if not pos and atr_val > 0:
            # Trading mode:
            # - trend: follow raw signal
            # - reverse: invert raw signal
            # - auto: trend in strong regimes, reverse in weak/choppy regimes
            strong_trend = (adx_val >= trend_min_adx) and (move24 >= trend_min_move_24h)
            effective_entry_mode = strat.entry_mode
            if mode == "auto":
                if strong_trend and entry_mode_strong:
                    effective_entry_mode = entry_mode_strong
                if (not strong_trend) and entry_mode_weak:
                    effective_entry_mode = entry_mode_weak
            sig = generate_signal(symbol, window, replace(strat, entry_mode=effective_entry_mode))
            if not sig:
                continue
            effective_reverse = False
            if mode == "reverse":
                effective_reverse = True
            elif mode == "trend":
                effective_reverse = False
            else:
                effective_reverse = not strong_trend

            if effective_reverse:
                # Optional guardrail: skip extreme bursts in reverse mode.
                if adx_val > reverse_max_adx or move24 > reverse_max_move_24h:
                    continue
                inv_side = "short" if sig.side.value == "long" else "long"
                sig = type("Sig", (), {"side": type("S", (), {"value": inv_side})(), "entry_price": sig.entry_price, "atr": sig.atr})
            # Entry uses tranche risk so that total (entry + add-ons) stays at risk.risk_percent.
            eff_risk = risk.risk_percent
            if mode == "auto":
                if strong_trend and (risk_percent_strong is not None):
                    eff_risk = float(risk_percent_strong)
                if (not strong_trend) and (risk_percent_weak is not None):
                    eff_risk = float(risk_percent_weak)
            eff_tranche_risk = tranche_risk_percent(eff_risk, risk.pyramids_max)
            rp0 = RiskParams(
                risk_percent=eff_tranche_risk,
                leverage=risk.leverage,
                rr=risk.rr,
                sl_atr_mult=risk.sl_atr_mult,
                trail_activate_atr_mult=risk.trail_activate_atr_mult,
                trail_dist_atr_mult=risk.trail_dist_atr_mult,
                pyramids_max=risk.pyramids_max,
                pyramid_trigger_atr_mult=risk.pyramid_trigger_atr_mult,
            )
            plan = build_order_plan(equity=equity, signal=sig, p=rp0)
            if not plan:
                continue
            # Apply slippage and taker fee on entry.
            entry_exec = _apply_slippage(plan.entry, sig.side.value, slippage_bps, is_entry=True)
            notional_entry = plan.qty * entry_exec
            fee_entry = _fee(notional_entry, taker_fee_rate)
            equity -= fee_entry
            pos = {
                "side": sig.side.value,
                "qty": plan.qty,
                "qty_open": plan.qty,
                "entry": plan.entry,
                "entry_exec": entry_exec,
                "sl": plan.sl,
                "tp": plan.tp,
                "tp1_done": False,
                "trailing_active": False,
                "trailing_sl": None,
            }
            pyramids_done = 0

    roi = (equity - 1000.0) / 1000.0
    gross_profit = float(np.sum([x for x in trade_pnls if x > 0])) if trade_pnls else 0.0
    gross_loss = float(-np.sum([x for x in trade_pnls if x < 0])) if trade_pnls else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    avg_trade_pnl = float(np.mean(trade_pnls)) if trade_pnls else 0.0
    sharpe_trades = 0.0
    if len(trade_pnls) >= 5:
        mu = float(np.mean(trade_pnls))
        sd = float(np.std(trade_pnls, ddof=1))
        sharpe_trades = (mu / sd) * math.sqrt(len(trade_pnls)) if sd > 0 else 0.0
    trades_per_month = 0.0
    if trade_months:
        unique_months = len(set(trade_months))
        trades_per_month = trades / unique_months if unique_months else 0.0

    return BtResult(symbol, trades, wins, losses, roi, max_dd, profit_factor, avg_trade_pnl, sharpe_trades, trades_per_month, monthly_pnl)


def _compute_dynamic_score(sub: pd.DataFrame, bars_per_day: int = 24) -> float:
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
    # Local score (without cross-sectional normalization): enough for ranking each day.
    return 2.0 * pump_count + 80.0 * atr_pct + 10.0 * brk


def backtest_portfolio_daily_reselect(
    symbol_dfs: dict[str, pd.DataFrame],
    strat: StrategyParams,
    risk: RiskParams,
    taker_fee_rate: float,
    slippage_bps: float,
    top_n: int,
    lookback_days: int,
    score_quantile: float = 0.75,
    funding_maps: dict[str, dict[int, float]] | None = None,
    mode: str = "auto",
    reverse_max_adx: float = 35.0,
    reverse_max_move_24h: float = 0.08,
    trend_min_adx: float = 30.0,
    trend_min_move_24h: float = 0.06,
    min_breadth_count: int = 6,
    neutral_min_breadth_count: int = 3,
    risk_percent_strong: float | None = None,
    risk_percent_neutral: float | None = None,
    risk_percent_weak: float | None = None,
    weak_disable_impulse_entries: bool = False,
    loss_streak_block_threshold: int = 0,
    loss_streak_block_hours: int = 0,
    entry_mode_strong: str | None = None,
    entry_mode_neutral: str | None = None,
    entry_mode_weak: str | None = None,
    trade_range_start: pd.Timestamp | None = None,
    trade_range_end: pd.Timestamp | None = None,
    bars_per_day: int = 24,
    staged_exits: bool = False,
    stage1_r: float = 1.0,
    stage1_close_ratio: float = 0.0,
    stage2_r: float = 3.0,
    stage2_close_ratio: float = 0.50,
    stage3_r: float = 5.0,
    staged_use_final_r: bool = False,
) -> PortfolioResult:
    start_equity = 1000.0
    equity = start_equity
    peak = equity
    max_dd = 0.0

    trades = 0
    wins = 0
    losses = 0
    trade_pnls: list[float] = []
    trade_months: list[str] = []
    monthly_pnl: dict[str, float] = {}
    symbol_pnl: dict[str, float] = {}
    symbol_monthly_pnl: dict[str, dict[str, float]] = {}

    positions: dict[str, dict[str, float | bool]] = {}
    applied_funding: dict[str, set[int]] = {s: set() for s in symbol_dfs.keys()}
    active_symbols: set[str] = set()
    regime_by_day: dict[object, str] = {}
    symbol_loss_streak: dict[str, int] = {}
    symbol_block_until: dict[str, pd.Timestamp] = {}
    last_day = None

    atr_cache = {s: atr_fn(df, strat.atr_period) for s, df in symbol_dfs.items()}
    adx_cache = {s: adx_fn(df, strat.adx_period) for s, df in symbol_dfs.items()}
    move24_cache = {s: df["close"].pct_change(bars_per_day).abs() for s, df in symbol_dfs.items()}

    timeline = sorted(set().union(*[set(df.index.tolist()) for df in symbol_dfs.values()]))
    if trade_range_start is not None:
        timeline = [t for t in timeline if pd.Timestamp(t) >= pd.Timestamp(trade_range_start)]
    if trade_range_end is not None:
        timeline = [t for t in timeline if pd.Timestamp(t) <= pd.Timestamp(trade_range_end)]
    tranche_risk = tranche_risk_percent(risk.risk_percent, risk.pyramids_max)

    for ts in timeline:
        day = ts.date()
        if day != last_day:
            last_day = day
            scored: list[tuple[str, float]] = []
            for sym, df in symbol_dfs.items():
                sub = df[df.index <= ts].tail(lookback_days * bars_per_day + 10)
                if sub.shape[0] < 120:
                    continue
                score = _compute_dynamic_score(sub, bars_per_day=bars_per_day)
                scored.append((sym, score))
            scored.sort(key=lambda x: x[1], reverse=True)
            if scored:
                scores = np.array([x[1] for x in scored], dtype=float)
                q = float(np.clip(score_quantile, 0.0, 1.0))
                cutoff = float(np.quantile(scores, q))
                selected = [(s, sc) for s, sc in scored if sc >= cutoff]
                if top_n > 0:
                    selected = selected[:top_n]
                active_symbols = {s for s, _ in selected}
            else:
                active_symbols = set()
            # 3-phase regime by market breadth on currently tradable universe.
            strong_count = 0
            for sym in active_symbols:
                df = symbol_dfs[sym]
                if ts not in df.index:
                    continue
                idx = df.index.get_loc(ts)
                if idx < 2:
                    continue
                adx_val = float(adx_cache[sym].iloc[idx]) if not pd.isna(adx_cache[sym].iloc[idx]) else 0.0
                move24 = float(move24_cache[sym].iloc[idx]) if not pd.isna(move24_cache[sym].iloc[idx]) else 0.0
                if (adx_val >= trend_min_adx) and (move24 >= trend_min_move_24h):
                    strong_count += 1
            if strong_count >= int(min_breadth_count):
                regime_by_day[day] = "strong"
            elif strong_count >= int(neutral_min_breadth_count):
                regime_by_day[day] = "neutral"
            else:
                regime_by_day[day] = "weak"

        # Update open positions first (funding, trailing, exits).
        for sym in list(positions.keys()):
            df = symbol_dfs[sym]
            if ts not in df.index:
                continue
            idx = df.index.get_loc(ts)
            if idx < 2:
                continue
            row = df.iloc[idx]
            last_close = float(row["close"])
            last_high = float(row["high"])
            last_low = float(row["low"])
            candle_open_ms = int(row.get("open_time_s", 0)) * 1000
            pos = positions[sym]

            # Funding
            fm = (funding_maps or {}).get(sym, {})
            if fm:
                for settle_ms, rate in fm.items():
                    if settle_ms in applied_funding[sym]:
                        continue
                    if settle_ms <= candle_open_ms:
                        notional = float(pos["qty_open"]) * last_close
                        funding = -notional * rate if pos["side"] == "long" else notional * rate
                        equity += funding
                        mkey = str(pd.to_datetime(settle_ms, unit="ms", utc=True).tz_convert(None).to_period("M"))
                        monthly_pnl[mkey] = monthly_pnl.get(mkey, 0.0) + float(funding)
                        symbol_pnl[sym] = symbol_pnl.get(sym, 0.0) + float(funding)
                        if sym not in symbol_monthly_pnl:
                            symbol_monthly_pnl[sym] = {}
                        symbol_monthly_pnl[sym][mkey] = symbol_monthly_pnl[sym].get(mkey, 0.0) + float(funding)
                        applied_funding[sym].add(settle_ms)

            atr_val = float(atr_cache[sym].iloc[idx]) if not pd.isna(atr_cache[sym].iloc[idx]) else 0.0
            # With "no 1R partial" staged profile, defer ATR trailing until after the 3R partial so the move can breathe.
            defer_trailing = bool(staged_exits) and (float(stage1_close_ratio) <= 0.0) and (
                not bool(pos.get("stage2_done", False))
            )
            if atr_val > 0 and not defer_trailing:
                if pos["side"] == "long":
                    profit = last_close - float(pos["entry"])
                    if (not bool(pos["trailing_active"])) and profit >= risk.trail_activate_atr_mult * atr_val:
                        pos["trailing_active"] = True
                        pos["trailing_sl"] = max(float(pos["sl"]), last_close - risk.trail_dist_atr_mult * atr_val)
                    elif bool(pos["trailing_active"]):
                        pos["trailing_sl"] = max(float(pos["trailing_sl"]), last_close - risk.trail_dist_atr_mult * atr_val)
                else:
                    profit = float(pos["entry"]) - last_close
                    if (not bool(pos["trailing_active"])) and profit >= risk.trail_activate_atr_mult * atr_val:
                        pos["trailing_active"] = True
                        pos["trailing_sl"] = min(float(pos["sl"]), last_close + risk.trail_dist_atr_mult * atr_val)
                    elif bool(pos["trailing_active"]):
                        pos["trailing_sl"] = min(float(pos["trailing_sl"]), last_close + risk.trail_dist_atr_mult * atr_val)

            sl_level = float(pos["trailing_sl"]) if bool(pos["trailing_active"]) else float(pos["sl"])
            if bool(pos.get("be_armed", False)):
                be = float(pos["entry_exec"])
                if pos["side"] == "long":
                    sl_level = max(sl_level, be)
                else:
                    sl_level = min(sl_level, be)

            hit = None
            exit_price = None
            if pos["side"] == "long":
                if last_low <= sl_level:
                    hit = "sl"
                    exit_price = sl_level
            else:
                if last_high >= sl_level:
                    hit = "sl"
                    exit_price = sl_level

            # Staged take-profits (1R / 3R / 5R) are processed only if SL did not hit this candle.
            if staged_exits and (hit is None):
                init_r = float(pos.get("initial_r", 0.0))
                if init_r > 0 and float(pos["qty_open"]) > 0:
                    e = float(pos["entry_exec"])
                    if pos["side"] == "long":
                        stage1_hit = (not bool(pos.get("stage1_done", False))) and (last_high >= e + stage1_r * init_r)
                        stage2_hit = (not bool(pos.get("stage2_done", False))) and (last_high >= e + stage2_r * init_r)
                        stage3_hit = (not bool(pos.get("stage3_done", False))) and (last_high >= e + stage3_r * init_r)
                    else:
                        stage1_hit = (not bool(pos.get("stage1_done", False))) and (last_low <= e - stage1_r * init_r)
                        stage2_hit = (not bool(pos.get("stage2_done", False))) and (last_low <= e - stage2_r * init_r)
                        stage3_hit = (not bool(pos.get("stage3_done", False))) and (last_low <= e - stage3_r * init_r)

                    def _close_partial(close_qty: float, raw_exit_price: float) -> float:
                        nonlocal equity
                        if close_qty <= 0:
                            return 0.0
                        exec_px = _apply_slippage(raw_exit_price, str(pos["side"]), slippage_bps, is_entry=False)
                        fee_px = _fee(close_qty * exec_px, taker_fee_rate)
                        pnl_px = (exec_px - e) * close_qty if pos["side"] == "long" else (e - exec_px) * close_qty
                        net_px = pnl_px - fee_px
                        equity += net_px
                        return net_px

                    if stage1_hit:
                        q = min(float(pos["qty_open"]), float(pos["qty"]) * float(stage1_close_ratio))
                        stage1_price = e + stage1_r * init_r if pos["side"] == "long" else e - stage1_r * init_r
                        net_px = _close_partial(q, stage1_price)
                        pos["qty_open"] = max(0.0, float(pos["qty_open"]) - q)
                        pos["stage1_done"] = True
                        if q > 0:
                            pos["be_armed"] = True
                            trade_pnls.append(net_px)
                            m = str(ts.tz_convert(None).to_period("M")) if getattr(ts, "tzinfo", None) is not None else str(ts.to_period("M"))
                            trade_months.append(m)
                            monthly_pnl[m] = monthly_pnl.get(m, 0.0) + float(net_px)
                            symbol_pnl[sym] = symbol_pnl.get(sym, 0.0) + float(net_px)
                            if sym not in symbol_monthly_pnl:
                                symbol_monthly_pnl[sym] = {}
                            symbol_monthly_pnl[sym][m] = symbol_monthly_pnl[sym].get(m, 0.0) + float(net_px)

                    if stage2_hit:
                        q = min(float(pos["qty_open"]), float(pos["qty"]) * float(stage2_close_ratio))
                        stage2_price = e + stage2_r * init_r if pos["side"] == "long" else e - stage2_r * init_r
                        net_px = _close_partial(q, stage2_price)
                        pos["qty_open"] = max(0.0, float(pos["qty_open"]) - q)
                        pos["stage2_done"] = True
                        pos["be_armed"] = True
                        pos["trailing_active"] = True
                        if q > 0:
                            trade_pnls.append(net_px)
                            m = str(ts.tz_convert(None).to_period("M")) if getattr(ts, "tzinfo", None) is not None else str(ts.to_period("M"))
                            trade_months.append(m)
                            monthly_pnl[m] = monthly_pnl.get(m, 0.0) + float(net_px)
                            symbol_pnl[sym] = symbol_pnl.get(sym, 0.0) + float(net_px)
                            if sym not in symbol_monthly_pnl:
                                symbol_monthly_pnl[sym] = {}
                            symbol_monthly_pnl[sym][m] = symbol_monthly_pnl[sym].get(m, 0.0) + float(net_px)

                    if bool(staged_use_final_r) and stage3_hit:
                        stage3_price = e + stage3_r * init_r if pos["side"] == "long" else e - stage3_r * init_r
                        pos["stage3_done"] = True
                        hit = "tp"
                        exit_price = stage3_price
            elif hit is None:
                # Legacy single TP behavior.
                tp_level = float(pos["tp"])
                if pos["side"] == "long":
                    if last_high >= tp_level:
                        hit = "tp"
                        exit_price = tp_level
                else:
                    if last_low <= tp_level:
                        hit = "tp"
                        exit_price = tp_level

            if hit:
                exec_exit = _apply_slippage(float(exit_price), str(pos["side"]), slippage_bps, is_entry=False)
                qty = float(pos["qty_open"])
                fee_exit = _fee(qty * exec_exit, taker_fee_rate)
                pnl = (exec_exit - float(pos["entry_exec"])) * qty if pos["side"] == "long" else (float(pos["entry_exec"]) - exec_exit) * qty
                net = pnl - fee_exit
                equity += net
                trades += 1
                if net >= 0:
                    wins += 1
                else:
                    losses += 1
                trade_pnls.append(net)
                m = str(ts.tz_convert(None).to_period("M")) if getattr(ts, "tzinfo", None) is not None else str(ts.to_period("M"))
                trade_months.append(m)
                monthly_pnl[m] = monthly_pnl.get(m, 0.0) + float(net)
                symbol_pnl[sym] = symbol_pnl.get(sym, 0.0) + float(net)
                if sym not in symbol_monthly_pnl:
                    symbol_monthly_pnl[sym] = {}
                symbol_monthly_pnl[sym][m] = symbol_monthly_pnl[sym].get(m, 0.0) + float(net)
                # Symbol kill-switch: block symbol after consecutive losing stop exits.
                if hit == "sl" and net < 0:
                    streak = int(symbol_loss_streak.get(sym, 0)) + 1
                    symbol_loss_streak[sym] = streak
                    if int(loss_streak_block_threshold) > 0 and streak >= int(loss_streak_block_threshold):
                        if int(loss_streak_block_hours) > 0:
                            symbol_block_until[sym] = ts + pd.Timedelta(hours=int(loss_streak_block_hours))
                elif net > 0:
                    symbol_loss_streak[sym] = 0
                    symbol_block_until.pop(sym, None)
                del positions[sym]
                peak = max(peak, equity)
                dd = (peak - equity) / peak if peak > 0 else 0.0
                max_dd = max(max_dd, dd)

        # Entries only for current active symbols.
        for sym in active_symbols:
            if sym in positions:
                continue
            block_until = symbol_block_until.get(sym)
            if block_until is not None and ts < block_until:
                continue
            df = symbol_dfs[sym]
            if ts not in df.index:
                continue
            idx = df.index.get_loc(ts)
            if idx < max(250, bars_per_day * 10):
                continue
            window = df.iloc[: idx + 1]
            adx_val = float(adx_cache[sym].iloc[idx]) if not pd.isna(adx_cache[sym].iloc[idx]) else 0.0
            move24 = float(move24_cache[sym].iloc[idx]) if not pd.isna(move24_cache[sym].iloc[idx]) else 0.0
            day_regime = regime_by_day.get(day, "weak")
            strong_trend = (day_regime == "strong")
            effective_entry_mode = strat.entry_mode
            if mode == "auto":
                if strong_trend and entry_mode_strong:
                    effective_entry_mode = entry_mode_strong
                elif day_regime == "neutral" and entry_mode_neutral:
                    effective_entry_mode = entry_mode_neutral
                elif (day_regime == "weak") and entry_mode_weak:
                    effective_entry_mode = entry_mode_weak
            if day_regime == "weak" and weak_disable_impulse_entries and effective_entry_mode in {"pump", "momentum", "hybrid"}:
                effective_entry_mode = "retest"
            sig = generate_signal(sym, window, replace(strat, entry_mode=effective_entry_mode))
            if not sig:
                continue
            effective_reverse = False
            if mode == "reverse":
                effective_reverse = True
            elif mode == "trend":
                effective_reverse = False
            else:
                effective_reverse = not strong_trend
            if effective_reverse and (adx_val > reverse_max_adx or move24 > reverse_max_move_24h):
                continue
            if effective_reverse:
                inv_side = "short" if sig.side.value == "long" else "long"
                sig = type("Sig", (), {"side": type("S", (), {"value": inv_side})(), "entry_price": sig.entry_price, "atr": sig.atr})

            eff_risk = risk.risk_percent
            if mode == "auto":
                if day_regime == "strong" and (risk_percent_strong is not None):
                    eff_risk = float(risk_percent_strong)
                elif day_regime == "neutral" and (risk_percent_neutral is not None):
                    eff_risk = float(risk_percent_neutral)
                elif day_regime == "weak" and (risk_percent_weak is not None):
                    eff_risk = float(risk_percent_weak)
            eff_tranche_risk = tranche_risk_percent(eff_risk, risk.pyramids_max)
            rp0 = RiskParams(
                risk_percent=eff_tranche_risk,
                leverage=risk.leverage,
                rr=risk.rr,
                sl_atr_mult=risk.sl_atr_mult,
                trail_activate_atr_mult=risk.trail_activate_atr_mult,
                trail_dist_atr_mult=risk.trail_dist_atr_mult,
                pyramids_max=risk.pyramids_max,
                pyramid_trigger_atr_mult=risk.pyramid_trigger_atr_mult,
            )
            plan = build_order_plan(equity=equity, signal=sig, p=rp0)
            if not plan:
                continue
            entry_exec = _apply_slippage(plan.entry, sig.side.value, slippage_bps, is_entry=True)
            fee_entry = _fee(plan.qty * entry_exec, taker_fee_rate)
            equity -= fee_entry
            positions[sym] = {
                "side": sig.side.value,
                "qty": plan.qty,
                "qty_open": plan.qty,
                "entry": plan.entry,
                "entry_exec": entry_exec,
                "sl": plan.sl,
                "tp": plan.tp,
                "trailing_active": False,
                "trailing_sl": plan.sl,
                "initial_r": abs(entry_exec - plan.sl),
                "be_armed": False,
                "stage1_done": False,
                "stage2_done": False,
                "stage3_done": False,
            }

    roi = (equity - start_equity) / start_equity
    gross_profit = float(np.sum([x for x in trade_pnls if x > 0])) if trade_pnls else 0.0
    gross_loss = float(-np.sum([x for x in trade_pnls if x < 0])) if trade_pnls else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    avg_trade_pnl = float(np.mean(trade_pnls)) if trade_pnls else 0.0
    sharpe_trades = 0.0
    if len(trade_pnls) >= 5:
        mu = float(np.mean(trade_pnls))
        sd = float(np.std(trade_pnls, ddof=1))
        sharpe_trades = (mu / sd) * math.sqrt(len(trade_pnls)) if sd > 0 else 0.0
    trades_per_month = 0.0
    if trade_months:
        trades_per_month = len(trade_months) / max(1, len(set(trade_months)))
    return PortfolioResult(
        start_equity=start_equity,
        end_equity=equity,
        trades=trades,
        wins=wins,
        losses=losses,
        roi=roi,
        max_dd=max_dd,
        profit_factor=profit_factor,
        avg_trade_pnl=avg_trade_pnl,
        sharpe_trades=sharpe_trades,
        trades_per_month=trades_per_month,
        monthly_pnl=monthly_pnl,
        symbol_pnl=symbol_pnl,
        symbol_monthly_pnl=symbol_monthly_pnl,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--limit", type=int, default=150, help="How many top symbols to backtest (for speed).")
    ap.add_argument("--fee", type=float, default=None, help="Override taker fee rate (e.g. 0.0004).")
    ap.add_argument("--slip-bps", type=float, default=None, help="Override slippage in bps per side (e.g. 2).")
    ap.add_argument("--funding", action="store_true", help="Include funding payments (MEXC futures).")
    ap.add_argument("--selector", choices=["turnover", "pump"], default="pump", help="Universe selector mode.")
    ap.add_argument("--include-majors", choices=["yes", "no"], default="yes", help="Allow BTC/ETH if they score well.")
    ap.add_argument("--reverse-signals", action="store_true", help="Invert strategy direction (long<->short).")
    ap.add_argument("--reverse-max-adx", type=float, default=35.0, help="Reverse-mode max ADX to avoid strong trend regimes.")
    ap.add_argument("--reverse-max-move-24h", type=float, default=0.08, help="Reverse-mode max abs 24h move.")
    ap.add_argument("--pyramids-max", type=int, default=0, help="Max add-ons per position. Use 0 for safer mode.")
    ap.add_argument("--partial-tp-ratio", type=float, default=0.5, help="Fraction to close at TP1 (e.g., 0.5).")
    ap.add_argument("--timeframe", choices=["1h", "15m"], default="1h", help="Backtest candle timeframe.")
    ap.add_argument("--entry-mode", choices=["retest", "momentum", "hybrid", "pump"], default="hybrid", help="Signal style.")
    ap.add_argument("--rr", type=float, default=3.0, help="Risk/reward ratio for TP distance.")
    ap.add_argument("--sl-atr-mult", type=float, default=1.0, help="Stop-loss distance in ATR multiples.")
    ap.add_argument("--trail-activate-atr-mult", type=float, default=1.5, help="Activate trailing stop after this ATR profit.")
    ap.add_argument("--trail-dist-atr-mult", type=float, default=0.5, help="Trailing stop distance in ATR multiples.")
    ap.add_argument("--mode", choices=["auto", "trend", "reverse"], default="auto", help="Directional mode.")
    ap.add_argument("--trend-min-adx", type=float, default=30.0, help="Auto-mode: min ADX for trend-follow mode.")
    ap.add_argument("--trend-min-move-24h", type=float, default=0.06, help="Auto-mode: min abs 24h move for trend-follow mode.")
    ap.add_argument("--min-breadth-count", type=int, default=6, help="Auto-mode: strong regime breadth threshold.")
    ap.add_argument("--neutral-min-breadth-count", type=int, default=3, help="Auto-mode: neutral regime breadth threshold.")
    ap.add_argument(
        "--entry-mode-strong",
        choices=["retest", "momentum", "hybrid", "pump"],
        default=None,
        help="Auto-mode: override entry mode in strong regime (e.g. pump).",
    )
    ap.add_argument(
        "--entry-mode-neutral",
        choices=["retest", "momentum", "hybrid", "pump"],
        default=None,
        help="Auto-mode: override entry mode in neutral regime.",
    )
    ap.add_argument(
        "--entry-mode-weak",
        choices=["retest", "momentum", "hybrid", "pump"],
        default=None,
        help="Auto-mode: override entry mode in weak regime (e.g. hybrid).",
    )
    ap.add_argument("--risk-percent-strong", type=float, default=None, help="Auto-mode: override risk percent in strong-trend regime.")
    ap.add_argument("--risk-percent-neutral", type=float, default=None, help="Auto-mode: override risk percent in neutral regime.")
    ap.add_argument("--risk-percent-weak", type=float, default=None, help="Auto-mode: override risk percent in weak/choppy regime.")
    ap.add_argument("--weak-disable-impulse-entries", action="store_true", help="In weak regime force retest-like entries (disable pump/momentum/hybrid impulse).")
    ap.add_argument("--loss-streak-block-threshold", type=int, default=0, help="Block symbol after this many consecutive losing SL exits.")
    ap.add_argument("--loss-streak-block-hours", type=int, default=0, help="Hours to block symbol after loss streak threshold.")
    ap.add_argument("--daily-reselect", action="store_true", help="Rebuild tradable symbol list every day.")
    ap.add_argument(
        "--daily-top-n",
        type=int,
        default=0,
        help="Cap symbols per day in daily-reselect mode. 0 means no hard cap (dynamic count by score quantile).",
    )
    ap.add_argument("--daily-lookback-days", type=int, default=14, help="Lookback for daily symbol scoring.")
    ap.add_argument(
        "--daily-score-quantile",
        type=float,
        default=0.75,
        help="Daily score quantile filter [0..1]. Keeps symbols with score >= quantile cutoff.",
    )
    ap.add_argument("--report-all-months", action="store_true", help="Print all months in range, including zero-PnL months.")
    ap.add_argument("--report-symbol-month", type=str, default="", help="Print per-symbol PnL for a month, e.g. 2026-02.")
    ap.add_argument("--strict-filters", action="store_true", help="Experimental: tighten entry filters to reduce trade count.")
    ap.add_argument(
        "--staged-exits",
        action="store_true",
        help="Staged exits: default no partial at 1R; 50%% at 3R + BE floor + trailing; remainder exits via trailing/SL (no hard 5R unless --staged-final-r).",
    )
    ap.add_argument("--stage1-r", type=float, default=1.0, help="R-multiple for optional stage1 partial (off when --stage1-close-ratio 0).")
    ap.add_argument("--stage1-close-ratio", type=float, default=0.0, help="Stage1 close fraction of initial qty (0 = skip 1R partial).")
    ap.add_argument("--stage2-r", type=float, default=3.0, help="R-multiple for stage2 partial close.")
    ap.add_argument("--stage2-close-ratio", type=float, default=0.50, help="Stage2 close fraction of initial qty.")
    ap.add_argument("--stage3-r", type=float, default=5.0, help="R-multiple for optional final limit close (only with --staged-final-r).")
    ap.add_argument(
        "--staged-final-r",
        action="store_true",
        help="Also force-close any remainder at stage3 R-multiple (legacy 5R target). Default is trailing-only for the remainder.",
    )
    ap.add_argument(
        "--universe-source",
        choices=["ticker", "detail"],
        default="detail",
        help="Universe source: ticker (active now) or detail (includes historical/offline).",
    )
    ap.add_argument(
        "--since",
        type=str,
        default="",
        help="Simulate from this UTC date (YYYY-MM-DD) inclusive. Loads extra warmup history automatically.",
    )
    ap.add_argument(
        "--until",
        type=str,
        default="",
        help="Simulate through this UTC date (YYYY-MM-DD) end-of-day inclusive. Default when --since set: now (UTC).",
    )
    ap.add_argument(
        "--warmup-days",
        type=int,
        default=45,
        help="Days of history before --since for EMA/ATR (default 45).",
    )
    args = ap.parse_args()
    bars_per_day = _bars_per_day(str(args.timeframe))

    if args.since.strip():
        since_d = _parse_iso_day_utc(args.since)
        if args.until.strip():
            until_d = _parse_iso_day_utc(args.until)
            end_ref = until_d.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc)
        else:
            end_ref = _utcnow()
        span_days = max(1, (end_ref - since_d).days + int(args.warmup_days) + 10)
        # fetch_futures_history loads [now-days, now]; must extend back to warmup start (since - warmup)
        hs_fetch = since_d - timedelta(days=max(1, int(args.warmup_days)))
        tail = _utcnow()
        depth_days = max(1, max(0, (tail - hs_fetch).days) + 14)
        args.days = max(int(args.days), span_days, depth_days)

    s = get_settings()
    client = MexcFuturesClient(base_url=s.mexc_base_url)
    cache = TopSymbolsCache(s.top_symbols_cache_path)
    if args.selector == "turnover":
        if args.universe_source == "detail":
            symbols = get_futures_contracts_from_detail(
                client=client,
                quote_asset=s.quote_asset,
                limit=max(args.limit, s.top_symbols_limit),
                include_majors=(args.include_majors == "yes"),
            )[: args.limit]
        else:
            symbols = get_top_futures_contracts_by_turnover(client, s.quote_asset, s.top_symbols_limit, cache)[: args.limit]
            symbols = [x for x in symbols if not any(y in x for y in ["USOIL", "UKOIL", "XAUT", "SILVER", "GOLD"])]
    else:
        # Build wider candidate pool, then rank by pump-history score.
        candidate_limit = max(args.limit * 3, 40)
        if args.universe_source == "detail":
            detail_syms = get_futures_contracts_from_detail(
                client=client,
                quote_asset=s.quote_asset,
                limit=max(candidate_limit, 200),
                include_majors=(args.include_majors == "yes"),
            )
            # Use empty turnover defaults; daily scoring uses price dynamics.
            candidates = [(sym, 1.0) for sym in detail_syms[:candidate_limit]]
        else:
            candidates = get_futures_candidates_with_turnover(
                client=client,
                quote_asset=s.quote_asset,
                limit=candidate_limit,
                include_majors=(args.include_majors == "yes"),
            )
        ticker_amount24 = {sym: amt for sym, amt in candidates}
        ranked = rank_symbols_by_pump_history(
            client=client,
            candidate_symbols=[sym for sym, _ in candidates],
            ticker_amount24=ticker_amount24,
            lookback_days=14,
        )
        symbols = [r.symbol for r in ranked[: args.limit]]

    if not symbols:
        print("No symbols selected after filters.")
        return

    print(f"Selector={args.selector} | include_majors={args.include_majors} | universe_source={args.universe_source}")
    print(f"Selected symbols ({len(symbols)}): {', '.join(symbols[:20])}")

    strat = StrategyParams(
        ema_period=200,
        adx_period=s.adx_period,
        adx_threshold=s.adx_threshold,
        atr_period=s.atr_period,
        atr_min_pct=s.atr_min_pct,
        boll_period=s.boll_period,
        boll_std=s.boll_std,
        pullback_lookback=10,
        entry_mode=args.entry_mode,
        pump_lookback=int(s.pump_lookback),
        pump_min_ret_1h=float(s.pump_min_ret_1h),
        pump_volume_mult=float(s.pump_volume_mult),
        pump_close_pos_min=float(s.pump_close_pos_min),
        pump_max_overext_atr=float(s.pump_max_overext_atr),
        pump_max_opp_wick_ratio=float(s.pump_max_opp_wick_ratio),
        pump_short_min_ret_1h=float(s.pump_short_min_ret_1h),
        pump_short_volume_mult=float(s.pump_short_volume_mult),
        pump_short_close_pos_min=float(s.pump_short_close_pos_min),
        pump_short_max_overext_atr=float(s.pump_short_max_overext_atr),
        pump_short_max_opp_wick_ratio=float(s.pump_short_max_opp_wick_ratio),
        pump_min_body_atr_long=float(s.pump_min_body_atr_long),
        pump_min_body_atr_short=float(s.pump_min_body_atr_short),
        pump_max_range_atr=float(s.pump_max_range_atr),
        pump_breakout_buffer_atr=float(s.pump_breakout_buffer_atr),
        pump_continuation_min_ratio_long=float(s.pump_continuation_min_ratio_long),
        pump_continuation_max_ratio_short=float(s.pump_continuation_max_ratio_short),
    )
    if bool(args.strict_filters):
        strat = replace(
            strat,
            adx_threshold=max(float(strat.adx_threshold), 28.0),
            atr_min_pct=max(float(strat.atr_min_pct), 0.006),
            pump_min_ret_1h=max(float(strat.pump_min_ret_1h), 0.020),
            pump_volume_mult=max(float(strat.pump_volume_mult), 1.8),
            pump_close_pos_min=max(float(strat.pump_close_pos_min), 0.66),
            pump_min_body_atr_long=max(float(strat.pump_min_body_atr_long), 0.36),
            pump_min_body_atr_short=max(float(strat.pump_min_body_atr_short), 0.42),
            pump_max_range_atr=min(float(strat.pump_max_range_atr), 2.6),
            pump_breakout_buffer_atr=max(float(strat.pump_breakout_buffer_atr), 0.12),
            pump_continuation_min_ratio_long=max(float(strat.pump_continuation_min_ratio_long), 0.995),
            pump_continuation_max_ratio_short=min(float(strat.pump_continuation_max_ratio_short), 1.008),
        )
    risk = RiskParams(
        risk_percent=s.risk_percent,
        leverage=s.leverage,
        rr=float(args.rr),
        sl_atr_mult=float(args.sl_atr_mult),
        trail_activate_atr_mult=float(args.trail_activate_atr_mult),
        trail_dist_atr_mult=float(args.trail_dist_atr_mult),
        pyramids_max=int(args.pyramids_max),
    )

    results: list[BtResult] = []
    fee = float(args.fee) if args.fee is not None else float(s.futures_taker_fee_rate)
    slip = float(args.slip_bps) if args.slip_bps is not None else float(s.slippage_bps)

    if args.daily_reselect:
        symbol_dfs: dict[str, pd.DataFrame] = {}
        funding_maps_all: dict[str, dict[int, float]] = {}
        for idx, sym in enumerate(symbols, start=1):
            try:
                print(f"[{idx}/{len(symbols)}] {sym} loading history for daily-reselect…", flush=True)
                df = load_cached_df(sym, args.days, timeframe=str(args.timeframe))
                if df is None or df.empty:
                    df = fetch_futures_history(client, sym, args.days, timeframe=str(args.timeframe))
                    if not df.empty:
                        save_cached_df(sym, args.days, df, timeframe=str(args.timeframe))
                if df.empty:
                    continue
                symbol_dfs[sym] = df
                if args.funding:
                    fm = load_cached_funding(sym, args.days)
                    if fm is None:
                        start_ms = int(df["open_time_s"].iloc[0]) * 1000
                        end_ms = int(df["open_time_s"].iloc[-1]) * 1000
                        fm = fetch_funding_rates(client, sym, start_ms=start_ms, end_ms=end_ms)
                        save_cached_funding(sym, args.days, fm)
                    funding_maps_all[sym] = fm or {}
            except Exception:
                continue

        if not symbol_dfs:
            print("No symbol data for daily reselect.")
            return

        trade_range_start: pd.Timestamp | None = None
        trade_range_end: pd.Timestamp | None = None
        if args.since.strip():
            since_d = _parse_iso_day_utc(args.since)
            trade_range_start = pd.Timestamp(since_d)
            wu = max(1, int(args.warmup_days))
            hs = pd.Timestamp(since_d - timedelta(days=wu))
            if args.until.strip():
                ud = _parse_iso_day_utc(args.until)
                trade_range_end = pd.Timestamp(
                    ud.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc)
                )
                he = trade_range_end
            else:
                trade_range_end = pd.Timestamp(_utcnow())
                he = trade_range_end
            symbol_dfs = _clip_symbol_dfs_history(symbol_dfs, hs.to_pydatetime(), he.to_pydatetime())
            print(
                f"Date window: trades from {trade_range_start} through {trade_range_end} "
                f"(history from {hs})",
                flush=True,
            )
            if not symbol_dfs:
                print("No symbol data left after date window clip.")
                return

        pr = backtest_portfolio_daily_reselect(
            symbol_dfs=symbol_dfs,
            strat=strat,
            risk=risk,
            taker_fee_rate=fee,
            slippage_bps=slip,
            top_n=int(args.daily_top_n),
            lookback_days=int(args.daily_lookback_days),
            score_quantile=float(args.daily_score_quantile),
            funding_maps=funding_maps_all if args.funding else None,
            mode=("reverse" if bool(args.reverse_signals) else args.mode),
            reverse_max_adx=float(args.reverse_max_adx),
            reverse_max_move_24h=float(args.reverse_max_move_24h),
            trend_min_adx=float(args.trend_min_adx),
            trend_min_move_24h=float(args.trend_min_move_24h),
            min_breadth_count=int(args.min_breadth_count),
            neutral_min_breadth_count=int(args.neutral_min_breadth_count),
            risk_percent_strong=(float(args.risk_percent_strong) if args.risk_percent_strong is not None else None),
            risk_percent_neutral=(float(args.risk_percent_neutral) if args.risk_percent_neutral is not None else None),
            risk_percent_weak=(float(args.risk_percent_weak) if args.risk_percent_weak is not None else None),
            weak_disable_impulse_entries=bool(args.weak_disable_impulse_entries),
            loss_streak_block_threshold=int(args.loss_streak_block_threshold),
            loss_streak_block_hours=int(args.loss_streak_block_hours),
            entry_mode_strong=(str(args.entry_mode_strong) if args.entry_mode_strong else None),
            entry_mode_neutral=(str(args.entry_mode_neutral) if args.entry_mode_neutral else None),
            entry_mode_weak=(str(args.entry_mode_weak) if args.entry_mode_weak else None),
            trade_range_start=trade_range_start,
            trade_range_end=trade_range_end,
            bars_per_day=bars_per_day,
            staged_exits=bool(args.staged_exits),
            stage1_r=float(args.stage1_r),
            stage1_close_ratio=float(args.stage1_close_ratio),
            stage2_r=float(args.stage2_r),
            stage2_close_ratio=float(args.stage2_close_ratio),
            stage3_r=float(args.stage3_r),
            staged_use_final_r=bool(args.staged_final_r),
        )
        winrate = (pr.wins / pr.trades) if pr.trades else 0.0
        print(
            f"Costs model: taker_fee={fee} | slippage_bps={slip} | tf={args.timeframe} "
            f"| mode={('reverse' if bool(args.reverse_signals) else args.mode)} "
            f"| daily_reselect=True top_n={int(args.daily_top_n)} lookback_days={int(args.daily_lookback_days)} "
            f"score_q={float(args.daily_score_quantile):.2f} "
            f"| strict_filters={bool(args.strict_filters)} | staged_exits={bool(args.staged_exits)}"
        )
        print(f"Portfolio trades: {pr.trades} | Winrate: {winrate*100:.1f}%")
        print(
            f"Portfolio ROI: {pr.roi*100:.2f}% | MaxDD: {pr.max_dd*100:.2f}% | PF: {pr.profit_factor:.2f} "
            f"| Start={pr.start_equity:.2f} End={pr.end_equity:.2f}"
        )
        print("")
        print("Top symbol PnL:")
        for sym, pnl in sorted(pr.symbol_pnl.items(), key=lambda x: x[1], reverse=True)[:10]:
            print(f"- {sym}: {pnl:+.2f} USD")
        if args.report_symbol_month:
            print("")
            print(f"Per-symbol PnL for {args.report_symbol_month} (USD):")
            month_rows: list[tuple[str, float]] = []
            for sym, by_month in pr.symbol_monthly_pnl.items():
                v = float(by_month.get(args.report_symbol_month, 0.0))
                if abs(v) > 1e-12:
                    month_rows.append((sym, v))
            if not month_rows:
                print("- no traded symbols with non-zero PnL for this month")
            else:
                for sym, pnl in sorted(month_rows, key=lambda x: x[1], reverse=True):
                    print(f"- {sym}: {pnl:+.2f} USD")
        if pr.monthly_pnl:
            print("")
            print("Monthly aggregate PnL (USD):")
            if args.report_all_months and symbol_dfs:
                all_min = min(df.index.min() for df in symbol_dfs.values() if not df.empty)
                all_max = max(df.index.max() for df in symbol_dfs.values() if not df.empty)
                start_m = all_min.tz_convert(None).to_period("M")
                end_m = all_max.tz_convert(None).to_period("M")
                m = start_m
                while m <= end_m:
                    k = str(m)
                    print(f"- {k}: {pr.monthly_pnl.get(k, 0.0):+.2f}")
                    m = m + 1
            else:
                for m in sorted(pr.monthly_pnl.keys()):
                    print(f"- {m}: {pr.monthly_pnl[m]:+.2f}")
        return

    for idx, sym in enumerate(symbols, start=1):
        try:
            print(f"[{idx}/{len(symbols)}] {sym} downloading 1h history…", flush=True)
            df = load_cached_df(sym, args.days, timeframe=str(args.timeframe))
            if df is None or df.empty:
                df = fetch_futures_history(client, sym, args.days, timeframe=str(args.timeframe))
                if not df.empty:
                    save_cached_df(sym, args.days, df, timeframe=str(args.timeframe))
            if df.empty:
                print(f"[{idx}/{len(symbols)}] {sym} skip: no candles", flush=True)
                continue
            funding_map = None
            if args.funding:
                funding_map = load_cached_funding(sym, args.days)
                if funding_map is None:
                    start_ms = int(df["open_time_s"].iloc[0]) * 1000
                    end_ms = int(df["open_time_s"].iloc[-1]) * 1000
                    print(f"[{idx}/{len(symbols)}] {sym} downloading funding history…", flush=True)
                    funding_map = fetch_funding_rates(client, sym, start_ms=start_ms, end_ms=end_ms)
                    save_cached_funding(sym, args.days, funding_map)
            r = backtest_symbol(
                sym,
                df,
                strat,
                risk,
                taker_fee_rate=fee,
                slippage_bps=slip,
                funding_by_settle_ms=funding_map,
                reverse_signals=bool(args.reverse_signals),
                reverse_max_adx=float(args.reverse_max_adx),
                reverse_max_move_24h=float(args.reverse_max_move_24h),
                partial_tp_ratio=float(args.partial_tp_ratio),
                mode=("reverse" if bool(args.reverse_signals) else args.mode),
                trend_min_adx=float(args.trend_min_adx),
                trend_min_move_24h=float(args.trend_min_move_24h),
                risk_percent_strong=(float(args.risk_percent_strong) if args.risk_percent_strong is not None else None),
                risk_percent_weak=(float(args.risk_percent_weak) if args.risk_percent_weak is not None else None),
                entry_mode_strong=(str(args.entry_mode_strong) if args.entry_mode_strong else None),
                entry_mode_weak=(str(args.entry_mode_weak) if args.entry_mode_weak else None),
                bars_per_day=bars_per_day,
            )
            results.append(r)
            print(
                f"[{idx}/{len(symbols)}] {sym} done: trades={r.trades} roi={r.roi*100:.2f}% mdd={r.max_dd*100:.2f}%",
                flush=True,
            )
        except Exception:
            print(f"[{idx}/{len(symbols)}] {sym} error -> skip", flush=True)
            continue

    results.sort(key=lambda x: x.roi, reverse=True)
    if not results:
        print("No results.")
        return

    avg_roi = float(np.mean([r.roi for r in results]))
    avg_dd = float(np.mean([r.max_dd for r in results]))
    total_trades = sum(r.trades for r in results)
    total_wins = sum(r.wins for r in results)
    winrate = (total_wins / total_trades) if total_trades else 0.0
    avg_pf = float(np.mean([r.profit_factor for r in results if np.isfinite(r.profit_factor)])) if results else 0.0
    avg_tpm = float(np.mean([r.trades_per_month for r in results])) if results else 0.0
    monthly_agg: dict[str, float] = {}
    for r in results:
        for m, v in r.monthly_pnl.items():
            monthly_agg[m] = monthly_agg.get(m, 0.0) + float(v)

    print(
        f"Costs model: taker_fee={fee} | slippage_bps={slip} | reverse_signals={bool(args.reverse_signals)} "
        f"| reverse_max_adx={float(args.reverse_max_adx):.1f} | reverse_max_move_24h={float(args.reverse_max_move_24h):.3f} "
        f"| entry_mode={args.entry_mode} | pyramids_max={int(args.pyramids_max)} | partial_tp_ratio={float(args.partial_tp_ratio):.2f} "
        f"| mode={( 'reverse' if bool(args.reverse_signals) else args.mode)} | trend_min_adx={float(args.trend_min_adx):.1f} "
        f"| trend_min_move_24h={float(args.trend_min_move_24h):.3f}"
    )
    print(f"Symbols tested: {len(results)}")
    print(f"Total trades: {total_trades} | Winrate: {winrate*100:.1f}%")
    print(f"Avg ROI: {avg_roi*100:.2f}% | Avg maxDD: {avg_dd*100:.2f}% | Avg PF: {avg_pf:.2f} | Avg trades/mo: {avg_tpm:.2f}")
    print("")
    print("Top 10:")
    for r in results[:10]:
        pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
        print(
            f"- {r.symbol}: trades={r.trades} win={r.wins}/{r.trades} "
            f"roi={r.roi*100:.2f}% mdd={r.max_dd*100:.2f}% pf={pf} tpm={r.trades_per_month:.2f}"
        )
    if monthly_agg:
        print("")
        print("Monthly aggregate PnL (sum across tested symbols, USD):")
        for m in sorted(monthly_agg.keys()):
            print(f"- {m}: {monthly_agg[m]:+.2f}")


if __name__ == "__main__":
    main()

