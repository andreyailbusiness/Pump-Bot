from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .exchange import MexcClient, MexcFuturesClient


def _safe_makedirs(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


@dataclass
class TopSymbolsCache:
    path: str

    def load(self) -> dict[str, Any] | None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def save(self, payload: dict[str, Any]) -> None:
        _safe_makedirs(self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_top_symbols_by_quote_volume(
    client: MexcClient,
    quote_asset: str,
    limit: int,
    cache: TopSymbolsCache,
    refresh_every: timedelta = timedelta(days=1),
) -> list[str]:
    """
    Builds top symbols list from public 24hr ticker.
    Filters by suffix == quote_asset (e.g. USDT) and uses quoteVolume (preferred) else volume.
    """
    cached = cache.load()
    if cached:
        try:
            ts = datetime.fromisoformat(cached["generated_at"])
            if _utcnow() - ts < refresh_every and isinstance(cached.get("symbols"), list):
                return [str(s) for s in cached["symbols"]]
        except Exception:
            pass

    tickers = client.ticker_24hr()
    filtered: list[tuple[str, float]] = []
    for t in tickers:
        sym = str(t.get("symbol", ""))
        if not sym.endswith(quote_asset):
            continue

        qv = t.get("quoteVolume")
        v = t.get("volume")
        try:
            score = float(qv) if qv is not None else float(v)
        except Exception:
            continue

        if score <= 0:
            continue
        filtered.append((sym, score))

    filtered.sort(key=lambda x: x[1], reverse=True)
    symbols = [s for s, _ in filtered[:limit]]

    cache.save({"generated_at": _utcnow().isoformat(), "quote_asset": quote_asset, "symbols": symbols})
    return symbols


def get_top_futures_contracts_by_turnover(
    client: MexcFuturesClient,
    quote_asset: str,
    limit: int,
    cache: TopSymbolsCache,
    refresh_every: timedelta = timedelta(days=1),
) -> list[str]:
    """
    Uses futures contract ticker `amount24` (turnover) as ranking metric.
    Contract symbols are like BTC_USDT.
    """
    cached = cache.load()
    if cached and cached.get("market") == "futures":
        try:
            ts = datetime.fromisoformat(cached["generated_at"])
            if _utcnow() - ts < refresh_every and isinstance(cached.get("symbols"), list):
                return [str(s) for s in cached["symbols"]]
        except Exception:
            pass

    tickers = client.contract_ticker()
    filtered: list[tuple[str, float]] = []
    for t in tickers:
        sym = str(t.get("symbol", ""))
        if not sym.endswith(f"_{quote_asset}"):
            continue
        try:
            score = float(t.get("amount24", 0) or 0)
        except Exception:
            continue
        if score <= 0:
            continue
        filtered.append((sym, score))

    filtered.sort(key=lambda x: x[1], reverse=True)
    symbols = [s for s, _ in filtered[:limit]]
    cache.save(
        {
            "generated_at": _utcnow().isoformat(),
            "quote_asset": quote_asset,
            "market": "futures",
            "symbols": symbols,
        }
    )
    return symbols


def get_futures_candidates_with_turnover(
    client: MexcFuturesClient,
    quote_asset: str,
    limit: int,
    include_majors: bool = True,
) -> list[tuple[str, float]]:
    """
    Returns ranked futures candidates as (symbol, amount24).
    Keeps crypto-perp style symbols and excludes obvious non-crypto contracts.
    """
    non_crypto_bases = {"USOIL", "UKOIL", "XAUT", "SILVER", "GOLD"}
    majors = {"BTC", "ETH"}

    out: list[tuple[str, float]] = []
    for t in client.contract_ticker():
        sym = str(t.get("symbol", ""))
        if not sym.endswith(f"_{quote_asset}"):
            continue
        base = sym[: -(len(quote_asset) + 1)]
        if base in non_crypto_bases:
            continue
        if (not include_majors) and (base in majors):
            continue
        try:
            amount24 = float(t.get("amount24", 0) or 0)
        except Exception:
            continue
        if amount24 <= 0:
            continue
        out.append((sym, amount24))

    out.sort(key=lambda x: x[1], reverse=True)
    return out[:limit]


def get_futures_contracts_from_detail(
    client: MexcFuturesClient,
    quote_asset: str,
    limit: int,
    include_majors: bool = True,
) -> list[str]:
    """
    Build symbol universe from contract detail endpoint (includes historical/offline contracts).
    This helps reduce survivorship bias in backtests.
    """
    non_crypto_bases = {"USOIL", "UKOIL", "XAUT", "SILVER", "GOLD", "SPX500", "DJ30", "NAS100"}
    majors = {"BTC", "ETH"}

    out: list[str] = []
    for d in client.contract_detail():
        sym = str(d.get("symbol", ""))
        if not sym.endswith(f"_{quote_asset}"):
            continue
        base = sym[: -(len(quote_asset) + 1)]
        if base in non_crypto_bases:
            continue
        if (not include_majors) and (base in majors):
            continue
        out.append(sym)

    # Preserve deterministic order and limit size.
    dedup = list(dict.fromkeys(out))
    return dedup[:limit]

