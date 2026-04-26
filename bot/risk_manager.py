from __future__ import annotations

from dataclasses import dataclass

from .strategy import Side, Signal


@dataclass(frozen=True)
class RiskParams:
    risk_percent: float = 0.01
    leverage: int = 1
    rr: float = 3.0  # Risk:Reward 1:3
    sl_atr_mult: float = 1.0
    trail_activate_atr_mult: float = 1.5
    trail_dist_atr_mult: float = 0.5
    # Pyramiding (trend add-ons)
    pyramids_max: int = 2
    pyramid_trigger_atr_mult: float = 1.0  # add each +1 ATR move in favor


@dataclass(frozen=True)
class OrderPlan:
    side: Side
    qty: float
    entry: float
    sl: float
    tp: float


def position_size_from_atr(equity: float, entry_price: float, atr_value: float, risk_percent: float, leverage: int) -> float:
    """
    Risk per trade: equity * risk_percent.
    SL distance: ATR * sl_atr_mult (handled by caller).
    Qty (base units) computed so that (qty * sl_dist) ~= risk_amount.
    Leverage affects margin requirements, not stop-based risk, so it is not applied here.
    """
    risk_amount = equity * risk_percent
    sl_dist = atr_value
    if sl_dist <= 0 or entry_price <= 0:
        return 0.0
    qty_base = risk_amount / sl_dist
    return max(qty_base, 0.0)


def build_order_plan(equity: float, signal: Signal, p: RiskParams) -> OrderPlan | None:
    entry = signal.entry_price
    if entry <= 0 or signal.atr <= 0:
        return None

    sl_dist = signal.atr * p.sl_atr_mult
    qty = position_size_from_atr(equity, entry, sl_dist, p.risk_percent, p.leverage)
    if qty <= 0:
        return None

    if signal.side == Side.LONG:
        sl = entry - sl_dist
        tp = entry + p.rr * sl_dist
    else:
        sl = entry + sl_dist
        tp = entry - p.rr * sl_dist

    return OrderPlan(side=signal.side, qty=qty, entry=entry, sl=sl, tp=tp)


def tranche_risk_percent(total_risk_percent: float, pyramids_max: int) -> float:
    """
    Split total risk across (1 + pyramids_max) tranches.
    Example: total 3% with 2 add-ons => 1% per tranche.
    """
    n = max(1, 1 + int(pyramids_max))
    return float(total_risk_percent) / n

