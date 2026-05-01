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


def _position_base_qty(pos: Position, qty: float | None = None) -> float:
    """Contracts × contract_size when set; else qty is base size (paper)."""
    q = float(pos.qty if qty is None else qty)
    cs = pos.contract_size
    if cs is not None and cs > 0:
        return q * float(cs)
    return q


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


@dataclass
class OrderManager:
    risk: RiskParams
    cooldown_hours: int = 48
    staged_exits: bool = True
    stage2_r: float = 3.0
    stage2_close_ratio: float = 0.5
    staged_final_r: bool = False
    stage3_r: float = 5.0

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

    def open_position_paper(
        self,
        state: BotState,
        signal: Signal,
        plan: OrderPlan,
        *,
        contract_size: float | None = None,
    ) -> None:
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
            qty_open=plan.qty,
            initial_r=abs(plan.entry - plan.sl),
            be_armed=False,
            stage2_done=False,
            contract_size=contract_size,
            unrealized_pnl_exchange=None,
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
        qty_open = float(pos.qty_open if pos.qty_open is not None else pos.qty)
        if qty_open <= 0:
            return

        defer_trailing = bool(self.staged_exits) and (pos.initial_r > 0) and (not bool(pos.stage2_done))
        if defer_trailing:
            return

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

        qty_open = float(pos.qty_open if pos.qty_open is not None else pos.qty)
        if qty_open <= 0:
            return

        sl_level = pos.trailing_sl if pos.trailing_active and pos.trailing_sl is not None else pos.sl
        if pos.be_armed:
            if pos.side == "long":
                sl_level = max(sl_level, pos.entry_price)
            else:
                sl_level = min(sl_level, pos.entry_price)
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
            elif self.staged_exits and (not pos.stage2_done) and pos.initial_r > 0 and hi >= (pos.entry_price + self.stage2_r * pos.initial_r):
                close_qty = min(qty_open, float(pos.qty) * float(self.stage2_close_ratio))
                if close_qty > 0:
                    stage_px = pos.entry_price + self.stage2_r * pos.initial_r
                    net = self._pnl_quote(pos, stage_px, qty=close_qty)
                    state.equity += net
                    pos.qty_open = max(0.0, qty_open - close_qty)
                    pos.stage2_done = True
                    pos.be_armed = True
                    pos.trailing_active = True
                    pos.trailing_sl = max(pos.sl, pos.entry_price)
                    state.trades.append(
                        {
                            "time": _iso(_utcnow()),
                            "type": "partial_close",
                            "symbol": symbol,
                            "side": pos.side,
                            "qty": close_qty,
                            "entry": pos.entry_price,
                            "exit": stage_px,
                            "pnl": net,
                            "reason": "stage2_tp",
                        }
                    )
                return
            elif (not self.staged_exits) and hi >= pos.tp:
                hit = "tp"
                exit_price = pos.tp
            elif self.staged_exits and self.staged_final_r and pos.initial_r > 0 and hi >= (pos.entry_price + self.stage3_r * pos.initial_r):
                hit = "tp"
                exit_price = pos.entry_price + self.stage3_r * pos.initial_r
        else:
            # Intrabar precedence: stop first, then take-profit.
            if hi >= sl_level:
                hit = "sl" if not pos.trailing_active else "trailing_sl"
                exit_price = sl_level
            elif self.staged_exits and (not pos.stage2_done) and pos.initial_r > 0 and lo <= (pos.entry_price - self.stage2_r * pos.initial_r):
                close_qty = min(qty_open, float(pos.qty) * float(self.stage2_close_ratio))
                if close_qty > 0:
                    stage_px = pos.entry_price - self.stage2_r * pos.initial_r
                    net = self._pnl_quote(pos, stage_px, qty=close_qty)
                    state.equity += net
                    pos.qty_open = max(0.0, qty_open - close_qty)
                    pos.stage2_done = True
                    pos.be_armed = True
                    pos.trailing_active = True
                    pos.trailing_sl = min(pos.sl, pos.entry_price)
                    state.trades.append(
                        {
                            "time": _iso(_utcnow()),
                            "type": "partial_close",
                            "symbol": symbol,
                            "side": pos.side,
                            "qty": close_qty,
                            "entry": pos.entry_price,
                            "exit": stage_px,
                            "pnl": net,
                            "reason": "stage2_tp",
                        }
                    )
                return
            elif (not self.staged_exits) and lo <= pos.tp:
                hit = "tp"
                exit_price = pos.tp
            elif self.staged_exits and self.staged_final_r and pos.initial_r > 0 and lo <= (pos.entry_price - self.stage3_r * pos.initial_r):
                hit = "tp"
                exit_price = pos.entry_price - self.stage3_r * pos.initial_r

        if not hit:
            return

        close_qty = float(pos.qty_open if pos.qty_open is not None else pos.qty)
        pnl = self._pnl_quote(pos, exit_price, qty=close_qty)
        state.equity += pnl
        state.trades.append(
            {
                "time": _iso(_utcnow()),
                "type": "close",
                "symbol": symbol,
                "side": pos.side,
                "qty": close_qty,
                "entry": pos.entry_price,
                "exit": exit_price,
                "pnl": pnl,
                "reason": hit,
            }
        )
        del state.positions[symbol]
        self.set_cooldown(state, symbol)

    def _pnl_quote(self, pos: Position, exit_price: float, qty: float | None = None) -> float:
        q = _position_base_qty(pos, qty)
        if pos.side == "long":
            return (exit_price - pos.entry_price) * q
        return (pos.entry_price - exit_price) * q

