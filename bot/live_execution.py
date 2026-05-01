from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import ccxt


def ccxt_symbol_to_internal(ccxt_symbol: str) -> str:
    """e.g. BTC/USDT:USDT -> BTC_USDT."""
    left = ccxt_symbol.split(":")[0]
    return left.replace("/", "_")


def extract_unrealized_pnl_usdt(p: dict[str, Any]) -> float | None:
    """
    MEXC/ccxt often leaves unified unrealizedPnl empty; same value may sit in raw info.
    """
    keys = (
        "unrealizedPnl",
        "unrealizedProfit",
        "unrealized",
        "upl",
        "profit",
        "pnl",
        "floatProfit",
        "holdProfit",
    )
    layers: list[dict[str, Any]] = [p]
    info = p.get("info")
    if isinstance(info, dict):
        layers.append(info)
    for layer in layers:
        for k in keys:
            raw = layer.get(k)
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    return None


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

    def place_reduce_trigger(
        self,
        symbol: str,
        position_side: str,
        amount: float,
        trigger_price: float,
        *,
        trigger_on: str,
    ) -> dict[str, Any]:
        """
        Place reduce-only trigger-market order on swap.
        trigger_on: "gte" (>=) or "lte" (<=).
        """
        m = self._market(symbol)
        close_side = "sell" if str(position_side).lower() == "long" else "buy"
        trig_type = 1 if trigger_on == "gte" else 2
        side_int = 4 if close_side == "sell" else 2  # one-way reduce-only: 4 close long, 2 close short
        req = {
            "symbol": m["id"],
            "vol": float(self.exchange.amount_to_precision(m["symbol"], float(amount))),
            "side": int(side_int),
            "type": 6,  # market
            "openType": 2,  # cross
            "triggerPrice": float(self.exchange.price_to_precision(m["symbol"], float(trigger_price))),
            "triggerType": int(trig_type),
            "executeCycle": 1,
            "trend": 1,
            "orderType": 5,  # trigger executes as market
        }
        rsp = self.exchange.contractPrivatePostPlanorderPlace(req)
        if not isinstance(rsp, dict):
            raise RuntimeError(f"planorder invalid response: {rsp!r}")
        code = rsp.get("code")
        success = rsp.get("success")
        ok_code = str(code) in {"0", "200"}
        ok_success = (success is None) or bool(success) is True
        oid = rsp.get("data")
        if not (ok_code and ok_success and oid):
            raise RuntimeError(f"planorder rejected: {rsp!r}")
        return {"id": str(oid), "info": rsp}

    def cancel_plan_order(self, order_id: str) -> None:
        if not order_id:
            return
        try:
            self.exchange.contractPrivatePostPlanorderCancel([str(order_id)])
        except Exception:
            # Best effort; stale/missing ids are common after trigger fill.
            return

    def fetch_open_positions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        rows = self.exchange.fetch_positions()
        for p in rows:
            contracts = float(p.get("contracts") or 0.0)
            if contracts <= 0:
                continue
            out.append(p)
        return out

    def fetch_position_entry_price(self, internal_symbol: str, retries: int = 8, sleep_sec: float = 0.8) -> float | None:
        """
        Read current exchange position entry price for a symbol (USDT-M swap).
        Useful right after market_open when create_order response lacks average fill.
        """
        target = str(internal_symbol).upper()
        for i in range(max(1, int(retries) + 1)):
            try:
                for p in self.fetch_open_positions():
                    sym = ccxt_symbol_to_internal(str(p.get("symbol", ""))).upper()
                    if sym != target:
                        continue
                    raw = p.get("entryPrice")
                    if raw is None:
                        raw = p.get("entry")
                    info = p.get("info")
                    if raw is None and isinstance(info, dict):
                        raw = info.get("openAvgPrice") or info.get("holdAvgPrice")
                    px = float(raw or 0.0)
                    if px > 0:
                        return px
            except Exception:
                pass
            if i < int(retries):
                time.sleep(float(max(0.0, sleep_sec)))
        return None

    def estimate_unrealized_usdt_swap(self, p: dict[str, Any], internal_symbol: str) -> float | None:
        """Linear USDT swap: (mark − entry) × contracts × contractSize in USDT terms."""
        try:
            m = self._market(internal_symbol)
            cs = float(m.get("contractSize") or 0.0)
            contracts = float(p.get("contracts") or 0.0)
            entry = float(p.get("entryPrice") or 0.0)
            side = str(p.get("side", "")).lower()
            if contracts <= 0 or entry <= 0 or cs <= 0:
                return None
            info = p.get("info") if isinstance(p.get("info"), dict) else {}
            mark: float | None = None
            for key in ("fairPrice", "indexPrice", "lastPrice"):
                raw = info.get(key)
                if raw is None:
                    continue
                try:
                    v = float(raw)
                    if v > 0:
                        mark = v
                        break
                except (TypeError, ValueError):
                    continue
            raw_mp = p.get("markPrice")
            if (mark is None or mark <= 0) and raw_mp is not None:
                try:
                    v = float(raw_mp)
                    if v > 0:
                        mark = v
                except (TypeError, ValueError):
                    pass
            if mark is None or mark <= 0:
                t = self.exchange.fetch_ticker(m["symbol"])
                for key in ("last", "close", "mark"):
                    if t.get(key) is not None:
                        try:
                            v = float(t[key])
                            if v > 0:
                                mark = v
                                break
                        except (TypeError, ValueError):
                            continue
            if mark is None or mark <= 0:
                return None
            base = contracts * cs
            if side == "long":
                return (mark - entry) * base
            return (entry - mark) * base
        except Exception:
            return None

    def unrealized_pnl_usdt_by_internal_symbol(self) -> dict[str, float]:
        """Fresh uPnL per internal symbol (exchange row or mark × contractSize)."""
        out: dict[str, float] = {}
        for p in self.fetch_open_positions():
            sym = ccxt_symbol_to_internal(str(p.get("symbol", "")))
            if not sym:
                continue
            u = extract_unrealized_pnl_usdt(p)
            if u is None:
                u = self.estimate_unrealized_usdt_swap(p, sym)
            if u is not None:
                out[sym] = u
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
