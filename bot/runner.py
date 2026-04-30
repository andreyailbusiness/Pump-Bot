from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

import pandas as pd

from .config import Settings
from .exchange import MexcClient, MexcFuturesClient
from .order_manager import OrderManager
from .risk_manager import RiskParams, build_order_plan
from .state import BotState, StateStore
from .strategy import StrategyParams, generate_signal
from .symbols import TopSymbolsCache, get_futures_candidates_with_turnover, get_top_symbols_by_quote_volume
from .telegram_notifier import TelegramNotifier


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _is_symbol_blocked(rt: BotRuntime, symbol: str) -> bool:
    until = rt.state.symbol_block_until.get(symbol)
    if not until:
        return False
    dt = _parse_iso(until)
    if dt is None:
        return False
    return datetime.now(timezone.utc) < dt


def _refresh_open_positions_last_price(rt: BotRuntime, symbols: Iterable[str]) -> None:
    symbols = list(symbols)
    if not symbols:
        return
    try:
        if isinstance(rt.client, MexcFuturesClient):
            ticker_rows = rt.client.contract_ticker()
            price_map: dict[str, float] = {}
            for row in ticker_rows:
                sym = str(row.get("symbol", ""))
                if not sym:
                    continue
                # Prefer mark/index over last trade for uPNL and SL checks on perps.
                raw_price = row.get("indexPrice")
                if raw_price is None:
                    raw_price = row.get("fairPrice")
                if raw_price is None:
                    raw_price = row.get("lastPrice")
                try:
                    if raw_price is not None:
                        price_map[sym] = float(raw_price)
                except Exception:
                    continue
            for sym in symbols:
                p = rt.state.positions.get(sym)
                if p is None:
                    continue
                if sym in price_map:
                    p.last_price = float(price_map[sym])
        else:
            ticker_rows = rt.client.ticker_24hr()  # type: ignore[union-attr]
            price_map: dict[str, float] = {}
            for row in ticker_rows:
                sym = str(row.get("symbol", ""))
                if not sym:
                    continue
                try:
                    price_map[sym] = float(row.get("lastPrice"))
                except Exception:
                    continue
            for sym in symbols:
                p = rt.state.positions.get(sym)
                if p is None:
                    continue
                spot_sym = sym.replace("_", "")
                if spot_sym in price_map:
                    p.last_price = float(price_map[spot_sym])
    except Exception:
        return


@dataclass
class BotRuntime:
    settings: Settings
    client: MexcClient | MexcFuturesClient
    state_store: StateStore
    state: BotState
    notifier: TelegramNotifier
    order_manager: OrderManager
    strat_params: StrategyParams

    def save(self) -> None:
        self.state_store.save(self.state)


def _last_closed(df: pd.DataFrame) -> pd.Series:
    return df.iloc[-2]


def _last_closed_time(df: pd.DataFrame) -> pd.Timestamp:
    return df.index[-2]


async def fetch_klines(client: MexcClient, symbol: str, interval: str, limit: int) -> pd.DataFrame:
    if isinstance(client, MexcFuturesClient):
        # Futures API uses start/end range with interval names like Min60.
        interval_map = {
            "1h": "Min60",
            "4h": "Hour4",
            "1d": "Day1",
            "1m": "Min1",
            "5m": "Min5",
            "15m": "Min15",
            "30m": "Min30",
        }
        fut_interval = interval_map.get(interval, "Min60")
        end_s = int(time.time())
        start_s = end_s - int(max(limit, 50) * 3600)
        return await asyncio.to_thread(client.contract_klines, symbol, fut_interval, start_s, end_s)
    return await asyncio.to_thread(client.klines, symbol, interval, limit)


async def scan_symbol(rt: BotRuntime, symbol: str) -> None:
    if symbol in rt.state.positions:
        return
    if rt.order_manager.in_cooldown(rt.state, symbol):
        return
    if rt.state.max_drawdown_reached(rt.settings.max_drawdown):
        return

    df = await fetch_klines(rt.client, symbol, rt.settings.timeframe, rt.settings.candles_limit)
    sig = generate_signal(symbol, df, rt.strat_params)
    if not sig:
        return

    plan = build_order_plan(
        equity=rt.state.equity,
        signal=sig,
        p=rt.order_manager.risk,
    )
    if not plan:
        return

    rt.order_manager.open_position_paper(rt.state, sig, plan)
    rt.save()

    rt.notifier.send(
        "\n".join(
            [
                f"📈 Signal: {symbol} {sig.side.value}",
                f"Entry: {plan.entry:.6g}",
                f"SL: {plan.sl:.6g}",
                f"TP: {plan.tp:.6g}",
                f"Qty: {plan.qty:.6g}",
                f"Reason: {sig.reason}",
            ]
        )
    )


async def scan_symbol_with_risk(rt: BotRuntime, symbol: str, risk_percent: float, entry_mode: str | None = None) -> None:
    """
    Open new positions with a regime-dependent risk percent.
    Existing positions are managed separately and are intentionally untouched.
    """
    if symbol in rt.state.positions:
        return
    if rt.order_manager.in_cooldown(rt.state, symbol):
        return
    if _is_symbol_blocked(rt, symbol):
        return
    if rt.state.max_drawdown_reached(rt.settings.max_drawdown):
        return

    df = await fetch_klines(rt.client, symbol, rt.settings.timeframe, rt.settings.candles_limit)
    strat = rt.strat_params if not entry_mode else replace(rt.strat_params, entry_mode=entry_mode)
    sig = generate_signal(symbol, df, strat)
    if not sig:
        return

    base_risk = rt.order_manager.risk
    open_risk = RiskParams(
        risk_percent=float(risk_percent),
        leverage=base_risk.leverage,
        rr=base_risk.rr,
        sl_atr_mult=base_risk.sl_atr_mult,
        trail_activate_atr_mult=base_risk.trail_activate_atr_mult,
        trail_dist_atr_mult=base_risk.trail_dist_atr_mult,
        pyramids_max=base_risk.pyramids_max,
        pyramid_trigger_atr_mult=base_risk.pyramid_trigger_atr_mult,
    )
    plan = build_order_plan(
        equity=rt.state.equity,
        signal=sig,
        p=open_risk,
    )
    if not plan:
        return

    rt.order_manager.open_position_paper(rt.state, sig, plan)
    rt.save()

    rt.notifier.send(
        "\n".join(
            [
                f"📈 Signal: {symbol} {sig.side.value}",
                f"Entry: {plan.entry:.6g}",
                f"SL: {plan.sl:.6g}",
                f"TP: {plan.tp:.6g}",
                f"Qty: {plan.qty:.6g}",
                f"Risk mode: {risk_percent*100:.2f}%",
                f"Entry mode: {strat.entry_mode}",
                f"Reason: {sig.reason}",
            ]
        )
    )


async def update_positions(rt: BotRuntime, symbols: Iterable[str]) -> None:
    if not symbols:
        return
    _refresh_open_positions_last_price(rt, symbols)

    for sym in list(symbols):
        try:
            before_n = len(rt.state.trades)
            df = await fetch_klines(rt.client, sym, rt.settings.timeframe, max(60, rt.settings.atr_period + 10))
            last = _last_closed(df)
            last_price = float(last["close"])
            candle_high = float(last["high"])
            candle_low = float(last["low"])

            # ATR from same df
            from .indicators import atr as atr_fn

            atr_series = atr_fn(df, rt.settings.atr_period)
            atr_value = float(atr_series.iloc[-2]) if not pd.isna(atr_series.iloc[-2]) else 0.0

            rt.order_manager.update_position_paper(rt.state, sym, last_price, atr_value)
            mark = rt.state.positions.get(sym)
            mark_px = float(mark.last_price) if mark and mark.last_price is not None else None
            rt.order_manager.maybe_close_position_paper(
                rt.state,
                sym,
                last_price,
                candle_high=candle_high,
                candle_low=candle_low,
                mark_price=mark_px,
            )
            if len(rt.state.trades) > before_n:
                last_trade = rt.state.trades[-1]
                if last_trade.get("type") == "close" and str(last_trade.get("symbol")) == sym:
                    reason = str(last_trade.get("reason", ""))
                    pnl = float(last_trade.get("pnl", 0.0) or 0.0)
                    streak = int(rt.state.symbol_loss_streak.get(sym, 0))
                    if reason in {"sl", "trailing_sl"} and pnl < 0:
                        streak += 1
                        rt.state.symbol_loss_streak[sym] = streak
                        if streak >= int(rt.settings.loss_streak_block_threshold):
                            block_until = datetime.now(timezone.utc) + timedelta(hours=int(rt.settings.loss_streak_block_hours))
                            rt.state.symbol_block_until[sym] = block_until.isoformat()
                    elif pnl > 0:
                        rt.state.symbol_loss_streak[sym] = 0
                        rt.state.symbol_block_until.pop(sym, None)
        except Exception:
            continue

    if rt.state.positions:
        _refresh_open_positions_last_price(rt, list(rt.state.positions.keys()))
    rt.save()


async def detect_market_regime(rt: BotRuntime, symbols: list[str]) -> tuple[str, int]:
    """
    Balanced regime detector:
    - symbol qualifies as strong if ADX >= TREND_MIN_ADX and |move24h| >= TREND_MIN_MOVE_24H
    - market is strong if count(qualifying symbols) >= MIN_BREADTH_COUNT
    """
    if not symbols:
        return "weak", 0

    from .indicators import adx as adx_fn

    probe_n = max(1, min(int(rt.settings.breadth_probe_symbols), len(symbols)))
    probe_symbols = symbols[:probe_n]
    strong_count = 0

    for sym in probe_symbols:
        try:
            df = await fetch_klines(rt.client, sym, rt.settings.timeframe, max(80, rt.settings.candles_limit))
            if df.shape[0] < 40:
                continue
            adx_series = adx_fn(df, rt.settings.adx_period)
            adx_val = float(adx_series.iloc[-2]) if not pd.isna(adx_series.iloc[-2]) else 0.0
            move_24h = df["close"].pct_change(24).abs()
            move24 = float(move_24h.iloc[-2]) if not pd.isna(move_24h.iloc[-2]) else 0.0
            if (adx_val >= rt.settings.trend_min_adx) and (move24 >= rt.settings.trend_min_move_24h):
                strong_count += 1
        except Exception:
            continue

    if strong_count >= int(rt.settings.min_breadth_count):
        regime = "strong"
    elif strong_count >= int(rt.settings.neutral_min_breadth_count):
        regime = "neutral"
    else:
        regime = "weak"
    return regime, strong_count


def build_runtime(settings: Settings) -> BotRuntime:
    client: MexcClient | MexcFuturesClient
    if settings.market_type == "futures":
        client = MexcFuturesClient(base_url=settings.mexc_base_url)
    else:
        client = MexcClient(base_url=settings.mexc_base_url)
    state_store = StateStore(path=settings.state_path)
    state = state_store.load()
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)

    risk = RiskParams(risk_percent=settings.risk_percent, leverage=settings.leverage)
    order_manager = OrderManager(
        risk=risk,
        cooldown_hours=settings.cooldown_hours,
        staged_exits=bool(settings.paper_staged_exits),
        stage2_r=float(settings.paper_stage2_r),
        stage2_close_ratio=float(settings.paper_stage2_close_ratio),
        staged_final_r=bool(settings.paper_staged_final_r),
        stage3_r=float(settings.paper_stage3_r),
    )

    strat_params = StrategyParams(
        ema_period=200,
        adx_period=settings.adx_period,
        adx_threshold=settings.adx_threshold,
        atr_period=settings.atr_period,
        atr_min_pct=settings.atr_min_pct,
        boll_period=settings.boll_period,
        boll_std=settings.boll_std,
        pullback_lookback=10,
        entry_mode=str(settings.strategy_entry_mode),
        pump_lookback=int(settings.pump_lookback),
        pump_min_ret_1h=float(settings.pump_min_ret_1h),
        pump_volume_mult=float(settings.pump_volume_mult),
        pump_close_pos_min=float(settings.pump_close_pos_min),
        pump_max_overext_atr=float(settings.pump_max_overext_atr),
        pump_max_opp_wick_ratio=float(settings.pump_max_opp_wick_ratio),
        pump_short_min_ret_1h=float(settings.pump_short_min_ret_1h),
        pump_short_volume_mult=float(settings.pump_short_volume_mult),
        pump_short_close_pos_min=float(settings.pump_short_close_pos_min),
        pump_short_max_overext_atr=float(settings.pump_short_max_overext_atr),
        pump_short_max_opp_wick_ratio=float(settings.pump_short_max_opp_wick_ratio),
        pump_min_body_atr_long=float(settings.pump_min_body_atr_long),
        pump_min_body_atr_short=float(settings.pump_min_body_atr_short),
        pump_max_range_atr=float(settings.pump_max_range_atr),
        pump_breakout_buffer_atr=float(settings.pump_breakout_buffer_atr),
        pump_continuation_min_ratio_long=float(settings.pump_continuation_min_ratio_long),
        pump_continuation_max_ratio_short=float(settings.pump_continuation_max_ratio_short),
    )

    return BotRuntime(
        settings=settings,
        client=client,
        state_store=state_store,
        state=state,
        notifier=notifier,
        order_manager=order_manager,
        strat_params=strat_params,
    )


async def bot_loop(rt: BotRuntime, poll_every: timedelta = timedelta(minutes=5)) -> None:
    cache = TopSymbolsCache(rt.settings.top_symbols_cache_path)

    while True:
        try:
            # Update existing positions first
            await update_positions(rt, list(rt.state.positions.keys()))

            if rt.settings.market_type == "futures" and isinstance(rt.client, MexcFuturesClient):
                # Keep universe crypto-focused; exclude commodities/index contracts.
                symbols = [
                    sym
                    for sym, _ in get_futures_candidates_with_turnover(
                        rt.client,
                        quote_asset=rt.settings.quote_asset,
                        limit=rt.settings.top_symbols_limit,
                        include_majors=True,
                    )
                ]
            else:
                symbols = get_top_symbols_by_quote_volume(
                    rt.client,  # type: ignore[arg-type]
                    quote_asset=rt.settings.quote_asset,
                    limit=rt.settings.top_symbols_limit,
                    cache=cache,
                    refresh_every=timedelta(days=1),
                )

            regime, breadth = await detect_market_regime(rt, symbols)
            if regime == "strong":
                entry_risk = float(rt.settings.risk_percent_strong)
                entry_mode = str(rt.settings.strategy_entry_mode_strong)
            elif regime == "neutral":
                entry_risk = float(rt.settings.risk_percent_neutral)
                entry_mode = str(rt.settings.strategy_entry_mode_neutral)
            else:
                entry_risk = float(rt.settings.risk_percent_weak)
                entry_mode = str(rt.settings.strategy_entry_mode_weak)
                if rt.settings.weak_disable_impulse_entries and entry_mode in {"pump", "momentum", "hybrid"}:
                    entry_mode = "retest"
            rt.state.market_regime = regime
            rt.state.regime_entry_risk = entry_risk
            rt.state.regime_breadth = int(breadth)
            rt.state.regime_universe = int(len(symbols))
            print(
                f"[risk-mode] regime={regime} breadth={breadth}/{len(symbols)} "
                f"entry_risk={entry_risk*100:.2f}% "
                f"entry_mode={entry_mode} "
                f"(thresholds adx>={rt.settings.trend_min_adx}, move24h>={rt.settings.trend_min_move_24h}, min_breadth={rt.settings.min_breadth_count})",
                flush=True,
            )

            # Scan sequentially to keep API usage conservative on free tiers.
            for sym in symbols:
                await scan_symbol_with_risk(rt, sym, entry_risk, entry_mode=entry_mode)

        except Exception:
            pass

        await asyncio.sleep(poll_every.total_seconds())

