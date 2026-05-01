from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import ccxt


def symbol_to_ccxt(symbol: str) -> str:
    # Internal futures symbol format is e.g. RAVE_USDT.
    if "_" in symbol:
        base, quote = symbol.split("_", 1)
        return f"{base}/{quote}:{quote}"
    return symbol


@dataclass
class LiveExecution:
    api_key: str
    api_secret: str
    timeout_ms: int = 20000

    def __post_init__(self) -> None:
        self.exchange = ccxt.mexc(
            {
                "apiKey": self.api_key,
                "secret": self.api_secret,
                "enableRateLimit": True,
                "timeout": self.timeout_ms,
                "options": {"defaultType": "swap"},
            }
        )
        self.exchange.load_markets()

    def _market(self, symbol: str) -> dict[str, Any]:
        ccxt_sym = symbol_to_ccxt(symbol)
        m = self.exchange.market(ccxt_sym)
        if not m:
            raise ValueError(f"Market not found: {symbol}")
        return m

    def normalize_amount(self, symbol: str, amount: float) -> float:
        m = self._market(symbol)
        ccxt_sym = m["symbol"]
        amt = float(self.exchange.amount_to_precision(ccxt_sym, max(0.0, float(amount))))
        min_amt = (((m.get("limits") or {}).get("amount") or {}).get("min")) or 0.0
        if amt < float(min_amt):
            return 0.0
        return amt

    def market_open(self, symbol: str, side: str, amount: float) -> dict[str, Any]:
        m = self._market(symbol)
        order = self.exchange.create_order(
            symbol=m["symbol"],
            type="market",
            side=("buy" if side == "long" else "sell"),
            amount=float(amount),
            params={"openType": 2},  # isolated
        )
        return order

    def market_reduce(self, symbol: str, side: str, amount: float) -> dict[str, Any]:
        m = self._market(symbol)
        order = self.exchange.create_order(
            symbol=m["symbol"],
            type="market",
            side=("sell" if side == "long" else "buy"),
            amount=float(amount),
            params={"reduceOnly": True, "openType": 2},
        )
        return order

    def fetch_open_positions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        rows = self.exchange.fetch_positions()
        for p in rows:
            contracts = float(p.get("contracts") or 0.0)
            if contracts <= 0:
                continue
            out.append(p)
        return out

    def fetch_futures_wallet_usdt(self) -> dict[str, Any]:
        """USDT balance on swap (USDT-M futures wallet)."""
        try:
            bal = self.exchange.fetch_balance({"type": "swap"})
        except Exception:
            bal = self.exchange.fetch_balance()
        usdt = None
        if isinstance(bal, dict):
            usdt = bal.get("USDT") or bal.get("usdt")
        out: dict[str, Any] = {"currency": "USDT"}
        if isinstance(usdt, dict):
            out["free"] = float(usdt.get("free") or 0.0)
            out["used"] = float(usdt.get("used") or 0.0)
            out["total"] = float(usdt.get("total") or 0.0)
        else:
            out["free"] = out["used"] = out["total"] = 0.0
        return out
