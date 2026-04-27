from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .risk_manager import OrderPlan, RiskParams
from .state import BotState, Position
from .strategy import Side, Signal


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


@dataclass
class OrderManager:
    risk: RiskParams
    cooldown_hours: int = 48

    def in_cooldown(self, state: BotState, symbol: str) -> bool:
        until = state.cooldown_until.get(symbol)
        if not until:
            return False
        try:
            return _utcnow() < _parse_iso(until)
        except Exception:
            return False

    def set_cooldown(self, state: BotState, symbol: str) -> None:
        state.cooldown_until[symbol] = _iso(_utcnow() + timedelta(hours=self.cooldown_hours))

    def open_position_paper(self, state: BotState, signal: Signal, plan: OrderPlan) -> None:
        if signal.symbol in state.positions:
            return
        pos = Position(
            symbol=signal.symbol,
            side=signal.side.value,
            qty=plan.qty,
            entry_price=plan.entry,
            entry_time=_iso(_utcnow()),
            sl=plan.sl,
            tp=plan.tp,
            trailing_active=False,
            trailing_sl=None,
            last_price=plan.entry,
        )
        state.positions[signal.symbol] = pos
        state.trades.append(
            {
                "time": _iso(_utcnow()),
                "type": "open",
                "symbol": signal.symbol,
                "side": signal.side.value,
                "qty": plan.qty,
                "entry": plan.entry,
                "sl": plan.sl,
                "tp": plan.tp,
                "reason": signal.reason,
            }
        )

    def update_position_paper(self, state: BotState, symbol: str, close_price: float, atr_value: float) -> None:
        pos = state.positions.get(symbol)
        if not pos:
            return

        # Do not set pos.last_price here: it is kept in sync with the exchange ticker for uPNL display.

        # Trailing activation at +1.5*ATR profit (uses last *closed* bar close for stability)
        if pos.side == "long":
            profit = close_price - pos.entry_price
            if (not pos.trailing_active) and profit >= self.risk.trail_activate_atr_mult * atr_value:
                pos.trailing_active = True
                pos.trailing_sl = max(pos.sl, close_price - self.risk.trail_dist_atr_mult * atr_value)
            elif pos.trailing_active and pos.trailing_sl is not None:
                pos.trailing_sl = max(pos.trailing_sl, close_price - self.risk.trail_dist_atr_mult * atr_value)
        else:
            profit = pos.entry_price - close_price
            if (not pos.trailing_active) and profit >= self.risk.trail_activate_atr_mult * atr_value:
                pos.trailing_active = True
                pos.trailing_sl = min(pos.sl, close_price + self.risk.trail_dist_atr_mult * atr_value)
            elif pos.trailing_active and pos.trailing_sl is not None:
                pos.trailing_sl = min(pos.trailing_sl, close_price + self.risk.trail_dist_atr_mult * atr_value)

    def maybe_close_position_paper(
        self,
        state: BotState,
        symbol: str,
        last_price: float,
        candle_high: float | None = None,
        candle_low: float | None = None,
        mark_price: float | None = None,
    ) -> None:
        pos = state.positions.get(symbol)
        if not pos:
            return

        sl_level = pos.trailing_sl if pos.trailing_active and pos.trailing_sl is not None else pos.sl
        hit: str | None = None
        exit_price = last_price
        hi = float(candle_high) if candle_high is not None else last_price
        lo = float(candle_low) if candle_low is not None else last_price
        if mark_price is not None:
            m = float(mark_price)
            hi = max(hi, m)
            lo = min(lo, m)

        if pos.side == "long":
            # Intrabar precedence: stop first, then take-profit.
            if lo <= sl_level:
                hit = "sl" if not pos.trailing_active else "trailing_sl"
                exit_price = sl_level
            elif hi >= pos.tp:
                hit = "tp"
                exit_price = pos.tp
        else:
            # Intrabar precedence: stop first, then take-profit.
            if hi >= sl_level:
                hit = "sl" if not pos.trailing_active else "trailing_sl"
                exit_price = sl_level
            elif lo <= pos.tp:
                hit = "tp"
                exit_price = pos.tp

        if not hit:
            return

        pnl = self._pnl_quote(pos, exit_price)
        state.equity += pnl
        state.trades.append(
            {
                "time": _iso(_utcnow()),
                "type": "close",
                "symbol": symbol,
                "side": pos.side,
                "qty": pos.qty,
                "entry": pos.entry_price,
                "exit": exit_price,
                "pnl": pnl,
                "reason": hit,
            }
        )
        del state.positions[symbol]
        self.set_cooldown(state, symbol)

    def _pnl_quote(self, pos: Position, exit_price: float) -> float:
        # Spot-like PnL in quote currency.
        if pos.side == "long":
            return (exit_price - pos.entry_price) * pos.qty
        return (pos.entry_price - exit_price) * pos.qty

