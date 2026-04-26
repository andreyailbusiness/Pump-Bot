from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd
import requests


KlineInterval = Literal["1h", "4h", "1d", "1m", "5m", "15m", "30m"]
FuturesInterval = Literal["Min1", "Min5", "Min15", "Min30", "Min60", "Hour4", "Hour8", "Day1", "Week1", "Month1"]


@dataclass(frozen=True)
class MexcClient:
    base_url: str = "https://api.mexc.com"
    timeout_s: int = 20

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url.rstrip("/") + path
        r = requests.get(url, params=params, timeout=self.timeout_s)
        r.raise_for_status()
        return r.json()

    def ticker_24hr(self) -> list[dict[str, Any]]:
        """
        Public endpoint.
        Returns list of tickers with quoteVolume, volume, lastPrice, symbol, etc.
        """
        data = self._get("/api/v3/ticker/24hr")
        if not isinstance(data, list):
            raise ValueError("Unexpected 24hr ticker response")
        return data

    def klines(
        self,
        symbol: str,
        interval: KlineInterval = "1h",
        limit: int = 300,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> pd.DataFrame:
        """
        Public endpoint. Returns OHLCV.
        MEXC spot kline: /api/v3/klines
        Response rows: [
          [openTime, open, high, low, close, volume, closeTime, quoteAssetVolume, trades, ...]
        ]
        """
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
        raw = self._get("/api/v3/klines", params=params)
        if not isinstance(raw, list) or len(raw) == 0:
            raise ValueError(f"Empty klines for {symbol}")

        rows = []
        for r in raw:
            rows.append(
                {
                    "open_time": int(r[0]),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]),
                    "close_time": int(r[6]),
                    "quote_volume": float(r[7]) if len(r) > 7 else None,
                    "trades": int(r[8]) if len(r) > 8 else None,
                }
            )

        df = pd.DataFrame(rows)
        df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.set_index("dt").sort_index()
        return df

    def now_ms(self) -> int:
        return int(time.time() * 1000)


@dataclass(frozen=True)
class MexcFuturesClient:
    base_url: str = "https://api.mexc.com"
    timeout_s: int = 20

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url.rstrip("/") + path
        r = requests.get(url, params=params, timeout=self.timeout_s)
        r.raise_for_status()
        return r.json()

    def contract_ticker(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """
        Public endpoint: GET /api/v1/contract/ticker
        Response sometimes returns object (single) or list (all). Normalize to list.
        """
        params = {"symbol": symbol} if symbol else None
        raw = self._get("/api/v1/contract/ticker", params=params)
        data = raw.get("data") if isinstance(raw, dict) else raw
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []

    def contract_detail(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """
        Public endpoint: GET /api/v1/contract/detail
        Can include active and inactive/offline contracts.
        """
        params = {"symbol": symbol} if symbol else None
        raw = self._get("/api/v1/contract/detail", params=params)
        if not isinstance(raw, dict) or not raw.get("success"):
            return []
        data = raw.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []

    def contract_klines(
        self,
        symbol: str,
        interval: FuturesInterval = "Min60",
        start_s: int | None = None,
        end_s: int | None = None,
    ) -> pd.DataFrame:
        """
        Public endpoint: GET /api/v1/contract/kline/{symbol}
        Response data fields: time/open/close/high/low/vol arrays
        Max returned points: 2000 per call.
        """
        params: dict[str, Any] = {"interval": interval}
        if start_s is not None:
            params["start"] = int(start_s)
        if end_s is not None:
            params["end"] = int(end_s)

        raw = self._get(f"/api/v1/contract/kline/{symbol}", params=params)
        if not isinstance(raw, dict) or not raw.get("success"):
            raise ValueError(f"Unexpected futures kline response for {symbol}")
        data = raw.get("data") or {}
        t = data.get("time") or []
        o = data.get("open") or []
        h = data.get("high") or []
        l = data.get("low") or []
        c = data.get("close") or []
        v = data.get("vol") or []
        if not t:
            return pd.DataFrame()

        df = pd.DataFrame(
            {
                "open_time_s": [int(x) for x in t],
                "open": [float(x) for x in o],
                "high": [float(x) for x in h],
                "low": [float(x) for x in l],
                "close": [float(x) for x in c],
                "volume": [float(x) for x in v],
            }
        )
        df["dt"] = pd.to_datetime(df["open_time_s"], unit="s", utc=True)
        df = df.set_index("dt").sort_index()
        return df

    def funding_rate_history(self, symbol: str, page_num: int = 1, page_size: int = 1000) -> list[dict[str, Any]]:
        """
        Public endpoint: GET /api/v1/contract/funding_rate/history
        Returns paginated records with fields: fundingRate, settleTime (ms), collectCycle (hours)
        """
        raw = self._get(
            "/api/v1/contract/funding_rate/history",
            params={"symbol": symbol, "page_num": page_num, "page_size": page_size},
        )
        if not isinstance(raw, dict) or not raw.get("success"):
            raise ValueError(f"Unexpected funding history response for {symbol}")
        data = raw.get("data") or {}
        return list(data.get("resultList") or [])

