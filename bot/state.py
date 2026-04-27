from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_makedirs(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


Side = Literal["long", "short"]


@dataclass
class Position:
    symbol: str
    side: Side
    qty: float
    entry_price: float
    entry_time: str
    sl: float
    tp: float
    trailing_active: bool = False
    trailing_sl: float | None = None
    last_price: float | None = None


@dataclass
class BotState:
    equity: float = 1000.0
    start_equity: float = 1000.0
    positions: dict[str, Position] = field(default_factory=dict)  # symbol -> position
    cooldown_until: dict[str, str] = field(default_factory=dict)  # symbol -> iso time
    symbol_block_until: dict[str, str] = field(default_factory=dict)  # symbol -> iso time (kill-switch)
    symbol_loss_streak: dict[str, int] = field(default_factory=dict)  # symbol -> consecutive stop-losses
    trades: list[dict[str, Any]] = field(default_factory=list)
    market_regime: str = "unknown"  # strong|neutral|weak|unknown
    regime_entry_risk: float = 0.0
    regime_breadth: int = 0
    regime_universe: int = 0
    updated_at: str = field(default_factory=_utcnow_iso)

    def max_drawdown_reached(self, max_dd: float) -> bool:
        if self.start_equity <= 0:
            return False
        # <=0: invalid/disabled; >=1: no halt (100% floor — tests / explicit off).
        if max_dd <= 0 or max_dd >= 1.0:
            return False
        dd = (self.start_equity - self.equity) / self.start_equity
        return dd >= max_dd


class StateStore:
    def __init__(self, path: str = "data/state.json"):
        self.path = path

    def load(self) -> BotState:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return self.from_dict(raw)
        except FileNotFoundError:
            return BotState()

    def save(self, state: BotState) -> None:
        state.updated_at = _utcnow_iso()
        _safe_makedirs(self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(state), f, ensure_ascii=False, indent=2)

    def to_dict(self, state: BotState) -> dict[str, Any]:
        d = asdict(state)
        d["positions"] = {k: asdict(v) for k, v in state.positions.items()}
        return d

    def from_dict(self, raw: dict[str, Any]) -> BotState:
        st = BotState(
            equity=float(raw.get("equity", 1000.0)),
            start_equity=float(raw.get("start_equity", raw.get("equity", 1000.0))),
            cooldown_until=dict(raw.get("cooldown_until", {}) or {}),
            symbol_block_until=dict(raw.get("symbol_block_until", {}) or {}),
            symbol_loss_streak={k: int(v) for k, v in dict(raw.get("symbol_loss_streak", {}) or {}).items()},
            trades=list(raw.get("trades", []) or []),
            market_regime=str(raw.get("market_regime", "unknown")),
            regime_entry_risk=float(raw.get("regime_entry_risk", 0.0)),
            regime_breadth=int(raw.get("regime_breadth", 0)),
            regime_universe=int(raw.get("regime_universe", 0)),
            updated_at=str(raw.get("updated_at", _utcnow_iso())),
        )
        positions_raw = raw.get("positions", {}) or {}
        for sym, p in positions_raw.items():
            st.positions[sym] = Position(**p)
        return st

