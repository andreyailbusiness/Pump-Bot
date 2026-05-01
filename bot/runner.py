from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import pandas as pd

from .config import Settings
from .exchange import MexcClient, MexcFuturesClient
from .live_execution import LiveExecution, ccxt_symbol_to_internal, extract_unrealized_pnl_usdt
from .order_manager import OrderManager
from .risk_manager import OrderPlan, RiskParams, build_order_plan
from .state import BotState, Position, StateStore
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


def _live_mode_active(rt: BotRuntime) -> bool:
    return bool(rt.settings.trading_mode == "live" and rt.settings.live_enabled and rt.live_exec is not None)


def apply_live_equity_from_wallet(rt: BotRuntime) -> None:
    """Keep BotState.equity aligned with USDT-M wallet so live position sizing matches real margin."""
    if rt.settings.trading_mode != "live" or rt.live_exec is None:
        return
    try:
        w = rt.live_exec.fetch_futures_wallet_usdt()
        total = float(w.get("total") or 0.0)
        if total <= 0:
            return
        # Template defaults from paper (1000/1000): reset drawdown baseline once to real wallet.
        if abs(rt.state.start_equity - 1000.0) < 0.02 and abs(rt.state.equity - 1000.0) < 0.02:
            rt.state.start_equity = total
        rt.state.equity = total
    except Exception:
        pass


def _mexc_exchange_position_sync_enabled(rt: BotRuntime) -> bool:
    """Use MEXC open positions as source of truth (requires live mode + API keys + ccxt client)."""
    if rt.live_exec is None or rt.settings.trading_mode != "live":
        return False
    return bool(rt.settings.mexc_exchange_position_sync)


def _daily_realized_pnl(state: BotState) -> float:
    today = datetime.now(timezone.utc).date()
    total = 0.0
    for t in state.trades:
        if t.get("type") not in {"close", "partial_close"}:
            continue
        ts = _parse_iso(str(t.get("time", "")))
        if ts is None:
            continue
        if ts.date() == today:
            total += float(t.get("pnl", 0.0) or 0.0)
    return total


def _contract_size_for_symbol(rt: BotRuntime, sym: str, p: dict[str, Any]) -> float | None:
    raw = p.get("contractSize")
    if raw is not None:
        try:
            cs = float(raw)
            if cs > 0:
                return cs
        except Exception:
            pass
    if rt.live_exec is not None:
        try:
            m = rt.live_exec._market(sym)
            cs = float(m.get("contractSize") or 0.0)
            return cs if cs > 0 else None
        except Exception:
            return None
    return None


def _apply_ccxt_open_position_row(rt: BotRuntime, p: dict[str, Any]) -> str | None:
    """Upsert one ccxt position row into state; return internal symbol if applied."""
    sym = ccxt_symbol_to_internal(str(p.get("symbol", "")))
    if not sym:
        return None
    contracts = float(p.get("contracts") or 0.0)
    if contracts <= 0:
        return None
    side = str(p.get("side", "")).lower()
    side = "long" if side == "long" else "short"
    entry = float(p.get("entryPrice") or p.get("entry") or p.get("markPrice") or 0.0)
    mark = float(p.get("markPrice") or p.get("lastPrice") or entry or 0.0)
    if entry <= 0:
        return None
    u_pnl = extract_unrealized_pnl_usdt(p)
    if u_pnl is None and rt.live_exec is not None:
        u_pnl = rt.live_exec.estimate_unrealized_usdt_swap(p, sym)
    c_size = _contract_size_for_symbol(rt, sym, p)
    pos = rt.state.positions.get(sym)
    if pos is None:
        # Exchange-restored position (e.g. after deploy) without known strategy context.
        # Keep it visible/synced, but don't invent synthetic SL/TP that could force-close it.
        rt.state.positions[sym] = Position(
            symbol=sym,
            side=side,  # type: ignore[arg-type]
            qty=contracts,
            entry_price=entry,
            entry_time=datetime.now(timezone.utc).isoformat(),
            sl=0.0,
            tp=0.0,
            trailing_active=False,
            trailing_sl=None,
            last_price=mark,
            qty_open=contracts,
            initial_r=0.0,
            be_armed=False,
            stage2_done=False,
            contract_size=c_size,
            unrealized_pnl_exchange=u_pnl,
        )
        pos = rt.state.positions[sym]
    pos.side = side  # type: ignore[assignment]
    pos.qty = contracts
    # Keep entry synced to exchange truth (critical after market fills / deploy restarts).
    pos.entry_price = entry
    pos.last_price = mark
    pos.contract_size = c_size
    pos.unrealized_pnl_exchange = u_pnl
    if pos.qty_open is None:
        pos.qty_open = contracts
    return sym


async def sync_live_positions(rt: BotRuntime) -> None:
    if not _mexc_exchange_position_sync_enabled(rt):
        return
    assert rt.live_exec is not None
    try:
        rows = await asyncio.to_thread(rt.live_exec.fetch_open_positions)
    except Exception as exc:
        if rt.settings.entry_block_log:
            print(f"[mexc-sync] fetch_open_positions failed: {exc}", flush=True)
        return

    seen: set[str] = set()
    for p in rows:
        sym = _apply_ccxt_open_position_row(rt, p)
        if sym:
            seen.add(sym)
    # Drop symbols that are not open on the exchange (survives deploys via MEXC truth).
    dropped: list[str] = []
    for sym in list(rt.state.positions.keys()):
        if sym not in seen:
            del rt.state.positions[sym]
            dropped.append(sym)
    if dropped and rt.settings.entry_block_log:
        print(f"[mexc-sync] removed stale local positions: {', '.join(dropped)}", flush=True)


@dataclass
class BotRuntime:
    settings: Settings
    client: MexcClient | MexcFuturesClient
    state_store: StateStore
    state: BotState
    notifier: TelegramNotifier
    order_manager: OrderManager
    strat_params: StrategyParams
    live_exec: LiveExecution | None = None

    def save(self) -> None:
        self.state_store.save(self.state)


def _effective_sl_level(pos: Position) -> float:
    sl_level = float(pos.trailing_sl) if pos.trailing_active and pos.trailing_sl is not None else float(pos.sl)
    if pos.be_armed:
        if pos.side == "long":
            sl_level = max(sl_level, float(pos.entry_price))
        else:
            sl_level = min(sl_level, float(pos.entry_price))
    return sl_level


def _has_runtime_risk_levels(pos: Position) -> bool:
    return float(pos.sl) > 0 and (float(pos.tp) > 0 or bool(pos.trailing_active))


def _desired_exchange_tp(rt: BotRuntime, pos: Position) -> tuple[float | None, float]:
    qty_open = float(pos.qty_open if pos.qty_open is not None else pos.qty)
    if qty_open <= 0:
        return None, 0.0
    if not rt.order_manager.staged_exits:
        return float(pos.tp), qty_open
    if pos.initial_r <= 0:
        return None, 0.0
    if not pos.stage2_done:
        stage2_px = (
            float(pos.entry_price) + float(rt.order_manager.stage2_r) * float(pos.initial_r)
            if pos.side == "long"
            else float(pos.entry_price) - float(rt.order_manager.stage2_r) * float(pos.initial_r)
        )
        stage2_qty = min(qty_open, float(pos.qty) * float(rt.order_manager.stage2_close_ratio))
        return stage2_px, max(stage2_qty, 0.0)
    if rt.order_manager.staged_final_r:
        stage3_px = (
            float(pos.entry_price) + float(rt.order_manager.stage3_r) * float(pos.initial_r)
            if pos.side == "long"
            else float(pos.entry_price) - float(rt.order_manager.stage3_r) * float(pos.initial_r)
        )
        return stage3_px, qty_open
    return None, 0.0


async def _cancel_live_protection_orders(rt: BotRuntime, symbol: str, pos: Position) -> None:
    if rt.live_exec is None:
        return
    for oid in (pos.sl_order_id, pos.tp_order_id):
        if oid:
            await asyncio.to_thread(rt.live_exec.cancel_plan_order, str(oid))
    pos.sl_order_id = None
    pos.tp_order_id = None
    pos.live_protect_qty = None
    pos.live_protect_sl = None
    pos.live_protect_tp = None


async def _sync_live_protection_orders(rt: BotRuntime, symbol: str, pos: Position, *, force: bool = False) -> None:
    if not _live_mode_active(rt):
        return
    assert rt.live_exec is not None
    # Positions restored from exchange without bot risk context: do not auto-arm trigger exits.
    if not _has_runtime_risk_levels(pos):
        await _cancel_live_protection_orders(rt, symbol, pos)
        return
    qty_open = float(pos.qty_open if pos.qty_open is not None else pos.qty)
    if qty_open <= 0:
        await _cancel_live_protection_orders(rt, symbol, pos)
        return
    qty_live = await asyncio.to_thread(rt.live_exec.normalize_amount, symbol, qty_open)
    if qty_live <= 0:
        await _cancel_live_protection_orders(rt, symbol, pos)
        return

    sl_px = float(_effective_sl_level(pos))
    tp_px_raw, tp_qty_raw = _desired_exchange_tp(rt, pos)
    tp_px: float | None = None
    tp_qty_live = 0.0
    if tp_px_raw is not None and tp_qty_raw > 0:
        tp_qty_live = await asyncio.to_thread(rt.live_exec.normalize_amount, symbol, min(tp_qty_raw, qty_open))
        if tp_qty_live > 0:
            tp_px = float(tp_px_raw)

    def _changed(a: float | None, b: float | None) -> bool:
        if a is None and b is None:
            return False
        if a is None or b is None:
            return True
        tol = max(1e-9, abs(a) * 1e-6, abs(b) * 1e-6)
        return abs(a - b) > tol

    need_rearm = bool(force)
    if not pos.sl_order_id:
        need_rearm = True
    if (tp_px is not None) != bool(pos.tp_order_id):
        need_rearm = True
    if tp_px is not None and not pos.tp_order_id:
        need_rearm = True
    if _changed(pos.live_protect_qty, float(qty_live)):
        need_rearm = True
    if _changed(pos.live_protect_sl, sl_px):
        need_rearm = True
    if _changed(pos.live_protect_tp, tp_px):
        need_rearm = True
    if not need_rearm:
        return

    await _cancel_live_protection_orders(rt, symbol, pos)

    sl_trigger_on = "lte" if pos.side == "long" else "gte"
    try:
        sl_order = await asyncio.to_thread(
            rt.live_exec.place_reduce_trigger,
            symbol,
            str(pos.side),
            float(qty_live),
            float(sl_px),
            trigger_on=sl_trigger_on,
        )
        pos.sl_order_id = str(sl_order.get("id") or "")
        if not pos.sl_order_id:
            raise RuntimeError(f"empty id in SL trigger response: {sl_order!r}")
    except Exception as exc:
        rt.notifier.send(f"⚠️ LIVE protection SL place failed: {symbol} err={exc}")
        if rt.settings.entry_block_log:
            print(f"[live-protect] SL place failed {symbol}: {exc}", flush=True)
        return

    if tp_px is not None and tp_qty_live > 0:
        tp_trigger_on = "gte" if pos.side == "long" else "lte"
        try:
            tp_order = await asyncio.to_thread(
                rt.live_exec.place_reduce_trigger,
                symbol,
                str(pos.side),
                float(tp_qty_live),
                float(tp_px),
                trigger_on=tp_trigger_on,
            )
            pos.tp_order_id = str(tp_order.get("id") or "")
            if not pos.tp_order_id:
                raise RuntimeError(f"empty id in TP trigger response: {tp_order!r}")
        except Exception as exc:
            rt.notifier.send(f"⚠️ LIVE protection TP place failed: {symbol} err={exc}")
            if rt.settings.entry_block_log:
                print(f"[live-protect] TP place failed {symbol}: {exc}", flush=True)
            pos.tp_order_id = None
    else:
        pos.tp_order_id = None

    pos.live_protect_qty = float(qty_live)
    pos.live_protect_sl = float(sl_px)
    pos.live_protect_tp = (float(tp_px) if tp_px is not None else None)


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
    if getattr(rt.state, "bot_paused", False):
        return
    if rt.settings.trading_mode == "live" and not rt.settings.live_enabled:
        # Live account + sync-only: manage exchange positions only, no paper fills.
        return
    if symbol in rt.state.positions:
        return
    if rt.order_manager.in_cooldown(rt.state, symbol):
        return
    if _is_symbol_blocked(rt, symbol):
        return
    if rt.state.max_drawdown_reached(rt.settings.max_drawdown):
        return
    if _live_mode_active(rt):
        if len(rt.state.positions) >= int(rt.settings.live_max_positions):
            return
        loss_lim = float(rt.settings.live_daily_loss_limit_usdt)
        if loss_lim > 0 and _daily_realized_pnl(rt.state) <= -loss_lim:
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
        if rt.settings.entry_block_log and _live_mode_active(rt):
            print(
                f"[entry-block] {symbol} signal={sig.side.value} but no order plan "
                f"(equity={rt.state.equity:.4f})",
                flush=True,
            )
        return

    contract_size: float | None = None
    if _live_mode_active(rt):
        assert rt.live_exec is not None
        entry_px = max(float(plan.entry), 1e-12)
        max_dev_atr = max(0.0, float(rt.settings.live_entry_max_deviation_atr))
        if max_dev_atr > 0 and isinstance(rt.client, MexcFuturesClient):
            try:
                rows = await asyncio.to_thread(rt.client.contract_ticker)
                mk: float | None = None
                for row in rows:
                    if str(row.get("symbol", "")) != symbol:
                        continue
                    raw = row.get("indexPrice")
                    if raw is None:
                        raw = row.get("fairPrice")
                    if raw is None:
                        raw = row.get("lastPrice")
                    if raw is not None:
                        mk = float(raw)
                    break
                if mk is not None and sig.atr > 0:
                    dev_atr = abs(float(mk) - float(entry_px)) / float(sig.atr)
                    if dev_atr > max_dev_atr:
                        if rt.settings.entry_block_log:
                            print(
                                f"[entry-block] {symbol} deviation too high: "
                                f"|mark-entry|/ATR={dev_atr:.3f} > {max_dev_atr:.3f}",
                                flush=True,
                            )
                        return
            except Exception:
                pass
        max_notional = max(0.0, float(rt.settings.live_max_notional_usdt))
        min_notional = max(0.0, float(rt.settings.live_min_order_notional_usdt))
        if max_notional <= 0:
            return
        risk_qty = float(plan.qty)
        max_qty = max_notional / entry_px
        min_qty_floor = (min_notional / entry_px) if min_notional > 0 else 0.0
        if min_qty_floor > max_qty + 1e-12:
            if rt.settings.entry_block_log:
                print(
                    f"[entry-block] {symbol} min order ~{min_notional} USDT needs qty>{max_qty:.8g} "
                    f"but LIVE_MAX_NOTIONAL_USDT={max_notional} caps size",
                    flush=True,
                )
            return
        capped_qty = min(max(risk_qty, min_qty_floor), max_qty)
        qty_live = await asyncio.to_thread(rt.live_exec.normalize_amount, symbol, capped_qty)
        if qty_live <= 0:
            if rt.settings.entry_block_log:
                print(
                    f"[entry-block] {symbol} qty_live=0 after normalize "
                    f"(capped_qty={capped_qty:.8g} entry≈{entry_px:.6g} max_notional={max_notional})",
                    flush=True,
                )
            return
        try:
            live_order = await asyncio.to_thread(rt.live_exec.market_open, symbol, sig.side.value, qty_live)
        except Exception as exc:
            rt.notifier.send(f"⚠️ LIVE open failed: {symbol} {sig.side.value} err={exc}")
            return

        # Use exchange entry price as source of truth; order response may omit average on MEXC.
        avg_px = float(live_order.get("average") or live_order.get("price") or 0.0)
        if avg_px <= 0:
            ex_entry = await asyncio.to_thread(rt.live_exec.fetch_position_entry_price, symbol, 8, 0.8)
            if ex_entry is not None and float(ex_entry) > 0:
                avg_px = float(ex_entry)
        if avg_px <= 0:
            avg_px = float(plan.entry)
        try:
            m = rt.live_exec._market(symbol)
            cs = float(m.get("contractSize") or 0.0)
            contract_size = cs if cs > 0 else None
        except Exception:
            contract_size = None
        plan = OrderPlan(
            side=plan.side,
            qty=float(qty_live),
            entry=float(avg_px),
            sl=float(avg_px - open_risk.sl_atr_mult * sig.atr if sig.side.value == "long" else avg_px + open_risk.sl_atr_mult * sig.atr),
            tp=float(avg_px + open_risk.rr * open_risk.sl_atr_mult * sig.atr if sig.side.value == "long" else avg_px - open_risk.rr * open_risk.sl_atr_mult * sig.atr),
        )

    rt.order_manager.open_position_paper(rt.state, sig, plan, contract_size=contract_size)
    if _live_mode_active(rt):
        pos = rt.state.positions.get(symbol)
        if pos is not None:
            await _sync_live_protection_orders(rt, symbol, pos, force=True)
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
            pre_pos = rt.state.positions.get(sym)
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
            if mark is not None and _has_runtime_risk_levels(mark):
                rt.order_manager.maybe_close_position_paper(
                    rt.state,
                    sym,
                    last_price,
                    candle_high=candle_high,
                    candle_low=candle_low,
                    mark_price=mark_px,
                )
            if len(rt.state.trades) > before_n:
                new_trades = rt.state.trades[before_n:]
                for tr in new_trades:
                    if str(tr.get("symbol")) != sym:
                        continue
                    ttype = str(tr.get("type", ""))
                    if _live_mode_active(rt) and ttype in {"close", "partial_close"}:
                        try:
                            qty_live = await asyncio.to_thread(
                                rt.live_exec.normalize_amount,  # type: ignore[union-attr]
                                sym,
                                float(tr.get("qty", 0.0) or 0.0),
                            )
                            if qty_live > 0:
                                await asyncio.to_thread(
                                    rt.live_exec.market_reduce,  # type: ignore[union-attr]
                                    sym,
                                    str(tr.get("side", "")),
                                    qty_live,
                                )
                        except Exception as exc:
                            rt.notifier.send(f"⚠️ LIVE close failed: {sym} type={ttype} err={exc}")
                        if ttype == "close" and pre_pos is not None:
                            await _cancel_live_protection_orders(rt, sym, pre_pos)
                        elif ttype == "partial_close":
                            cur_pos = rt.state.positions.get(sym)
                            if cur_pos is not None:
                                await _sync_live_protection_orders(rt, sym, cur_pos, force=True)

                last_trade = new_trades[-1]
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
            if _live_mode_active(rt):
                cur = rt.state.positions.get(sym)
                if cur is not None:
                    await _sync_live_protection_orders(rt, sym, cur, force=False)
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
    live_exec: LiveExecution | None = None
    # ccxt client for wallet / positions whenever futures keys exist (works in paper too for dashboard).
    # Live orders and exchange position sync remain gated by trading_mode / LIVE_ENABLED elsewhere.
    if settings.market_type == "futures" and settings.mexc_api_key and settings.mexc_api_secret:
        try:
            live_exec = LiveExecution(
                api_key=str(settings.mexc_api_key),
                api_secret=str(settings.mexc_api_secret),
            )
            print("[mexc] ccxt initialized (wallet API; live trading only if TRADING_MODE=live)", flush=True)
        except Exception as exc:
            print(f"[mexc] ccxt init failed: {exc}", flush=True)
    elif settings.market_type == "futures":
        print("[mexc] MARKET_TYPE=futures but MEXC_API_KEY/MEXC_API_SECRET missing.", flush=True)

    risk = RiskParams(
        risk_percent=settings.risk_percent,
        leverage=settings.leverage,
        rr=float(settings.rr),
        sl_atr_mult=float(settings.sl_atr_mult),
        trail_activate_atr_mult=float(settings.trail_activate_atr_mult),
        trail_dist_atr_mult=float(settings.trail_dist_atr_mult),
    )
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
    if bool(settings.strict_filters_live):
        strat_params = replace(
            strat_params,
            adx_threshold=max(float(strat_params.adx_threshold), 28.0),
            atr_min_pct=max(float(strat_params.atr_min_pct), 0.006),
            pump_min_ret_1h=max(float(strat_params.pump_min_ret_1h), 0.020),
            pump_volume_mult=max(float(strat_params.pump_volume_mult), 1.8),
            pump_close_pos_min=max(float(strat_params.pump_close_pos_min), 0.66),
            pump_min_body_atr_long=max(float(strat_params.pump_min_body_atr_long), 0.36),
            pump_min_body_atr_short=max(float(strat_params.pump_min_body_atr_short), 0.42),
            pump_max_range_atr=min(float(strat_params.pump_max_range_atr), 2.6),
            pump_breakout_buffer_atr=max(float(strat_params.pump_breakout_buffer_atr), 0.12),
            pump_continuation_min_ratio_long=max(float(strat_params.pump_continuation_min_ratio_long), 0.995),
            pump_continuation_max_ratio_short=min(float(strat_params.pump_continuation_max_ratio_short), 1.008),
        )

    return BotRuntime(
        settings=settings,
        client=client,
        state_store=state_store,
        state=state,
        notifier=notifier,
        order_manager=order_manager,
        strat_params=strat_params,
        live_exec=live_exec,
    )


async def bot_loop(rt: BotRuntime) -> None:
    poll_every = timedelta(seconds=int(rt.settings.bot_poll_interval_sec))
    cache = TopSymbolsCache(rt.settings.top_symbols_cache_path)

    while True:
        try:
            await sync_live_positions(rt)
            await asyncio.to_thread(apply_live_equity_from_wallet, rt)
            if getattr(rt.state, "bot_paused", False):
                rt.save()
                await asyncio.sleep(poll_every.total_seconds())
                continue
            if rt.settings.trading_mode == "live" and not rt.settings.live_enabled:
                # Sync-only mode: keep state aligned with exchange, but do not open/close anything.
                rt.save()
                await asyncio.sleep(poll_every.total_seconds())
                continue
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

            if _live_mode_active(rt):
                loss_lim = float(rt.settings.live_daily_loss_limit_usdt)
                if loss_lim > 0:
                    pnl_d = _daily_realized_pnl(rt.state)
                    if pnl_d <= -loss_lim:
                        print(
                            f"[risk] daily realized PnL {pnl_d:.2f} USDT — new entries blocked "
                            f"(LIVE_DAILY_LOSS_LIMIT_USDT={loss_lim}); set to 0 to disable",
                            flush=True,
                        )

            # Scan sequentially to keep API usage conservative on free tiers.
            for sym in symbols:
                await scan_symbol_with_risk(rt, sym, entry_risk, entry_mode=entry_mode)

        except Exception:
            pass

        await asyncio.sleep(poll_every.total_seconds())

