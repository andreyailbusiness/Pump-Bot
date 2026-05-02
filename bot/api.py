from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .state import BotState, Position, StateStore


def _position_upnl(
    side: str,
    qty: float,
    entry_price: float,
    last_price: float | None,
    contract_size: float | None = None,
) -> float | None:
    """USDT PnL; qty is contracts when contract_size is set (base per contract), else qty is base units (paper)."""
    if last_price is None:
        return None
    base_qty = qty * float(contract_size) if contract_size and contract_size > 0 else qty
    if side == "long":
        return (last_price - entry_price) * base_qty
    return (entry_price - last_price) * base_qty


def _aggregate_trade_statistics(trades: list[dict[str, Any]], *, since_iso: str | None = None) -> dict[str, Any]:
    """Summarize bot journal trades (open / partial_close / close)."""
    cutoff: datetime | None = None
    if since_iso:
        try:
            cutoff = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        except Exception:
            cutoff = None

    def _in_window(t: dict[str, Any]) -> bool:
        if cutoff is None:
            return True
        raw = t.get("time")
        if not raw:
            return True
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")) >= cutoff
        except Exception:
            return True

    rows = [t for t in trades if isinstance(t, dict) and _in_window(t)]

    opens = sum(1 for t in rows if str(t.get("type", "")) == "open")
    partials = [t for t in rows if str(t.get("type", "")) == "partial_close"]
    closes = [t for t in rows if str(t.get("type", "")) == "close"]

    def _pnl(t: dict[str, Any]) -> float:
        try:
            return float(t.get("pnl", 0.0) or 0.0)
        except Exception:
            return 0.0

    pnl_partials = sum(_pnl(t) for t in partials)
    pnl_closes = sum(_pnl(t) for t in closes)
    total_realized = pnl_partials + pnl_closes

    wins = sum(1 for t in closes if _pnl(t) > 0)
    losses = sum(1 for t in closes if _pnl(t) < 0)
    flat = sum(1 for t in closes if _pnl(t) == 0)
    decided = wins + losses
    winrate = (wins / decided) if decided > 0 else None

    gross_profit = sum(_pnl(t) for t in closes if _pnl(t) > 0)
    gross_loss = -sum(_pnl(t) for t in closes if _pnl(t) < 0)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
        profit_factor_unbounded = False
    elif gross_profit > 0:
        profit_factor = None
        profit_factor_unbounded = True
    else:
        profit_factor = None
        profit_factor_unbounded = False

    by_reason: dict[str, int] = {}
    for t in closes:
        r = str(t.get("reason", "") or "unknown")
        by_reason[r] = by_reason.get(r, 0) + 1

    return {
        "since": since_iso,
        "opens": opens,
        "partial_closes": len(partials),
        "full_closes": len(closes),
        "wins": wins,
        "losses": losses,
        "breakeven_closes": flat,
        "winrate": winrate,
        "total_realized_pnl": total_realized,
        "pnl_from_partials": pnl_partials,
        "pnl_from_full_closes": pnl_closes,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "profit_factor_unbounded": profit_factor_unbounded,
        "by_close_reason": by_reason,
    }


def _position_upnl_for_api(v: Position) -> float | None:
    if getattr(v, "unrealized_pnl_exchange", None) is not None:
        return float(v.unrealized_pnl_exchange)
    return _position_upnl(
        side=str(v.side),
        qty=float(v.qty),
        entry_price=float(v.entry_price),
        last_price=(float(v.last_price) if v.last_price is not None else None),
        contract_size=getattr(v, "contract_size", None),
    )


def create_app(
    state_store: StateStore,
    get_state: Callable[[], BotState],
    set_state: Callable[[BotState], None],
    max_drawdown_limit: float = 0.08,
    on_state_persisted: Callable[[], None] | None = None,
    fetch_mexc_wallet: Callable[[], dict[str, Any]] | None = None,
    set_bot_paused: Callable[[bool], None] | None = None,
    get_dashboard_meta: Callable[[], dict[str, Any]] | None = None,
    get_live_unrealized_usdt: Callable[[], dict[str, float]] | None = None,
) -> FastAPI:
    app = FastAPI(title="Instarding Bot", version="0.1.0")

    ui_dir = os.path.join(os.getcwd(), "ui")
    if os.path.isdir(ui_dir):
        app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")

    @app.get("/")
    def root() -> Any:
        index_path = os.path.join(ui_dir, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        return {"ok": True, "ui": "/ui"}

    @app.get("/api/state")
    def api_state() -> Any:
        st = get_state()
        d = asdict(st)
        live_upnl: dict[str, float] = {}
        if get_live_unrealized_usdt is not None:
            try:
                live_upnl = get_live_unrealized_usdt()
            except Exception:
                live_upnl = {}
        out_positions: dict[str, Any] = {}
        for k, v in st.positions.items():
            p = asdict(v)
            if k in live_upnl:
                p["upnl"] = live_upnl[k]
            else:
                p["upnl"] = _position_upnl_for_api(v)
            out_positions[k] = p
        d["positions"] = out_positions
        d["max_drawdown_limit"] = float(max_drawdown_limit)
        if get_dashboard_meta is not None:
            d["dashboard"] = get_dashboard_meta()
        return d

    @app.get("/api/positions")
    def api_positions() -> Any:
        st = get_state()
        live_upnl: dict[str, float] = {}
        if get_live_unrealized_usdt is not None:
            try:
                live_upnl = get_live_unrealized_usdt()
            except Exception:
                live_upnl = {}
        out_positions: dict[str, Any] = {}
        for k, v in st.positions.items():
            p = asdict(v)
            if k in live_upnl:
                p["upnl"] = live_upnl[k]
            else:
                p["upnl"] = _position_upnl_for_api(v)
            out_positions[k] = p
        return out_positions

    @app.get("/api/trades")
    def api_trades(limit: int = 200) -> Any:
        st = get_state()
        return list(reversed(st.trades[-limit:]))

    @app.get("/api/stats")
    def api_stats(
        since_days: int | None = Query(
            default=None,
            ge=1,
            le=3650,
            description="If set, only trades with time >= now-UTC since_days are counted.",
        ),
    ) -> Any:
        st = get_state()
        since_iso: str | None = None
        if since_days is not None:
            since_iso = (datetime.now(timezone.utc) - timedelta(days=int(since_days))).isoformat()
        return _aggregate_trade_statistics(list(st.trades), since_iso=since_iso)

    @app.get("/api/mexc/wallet")
    def api_mexc_wallet() -> Any:
        if fetch_mexc_wallet is None:
            return {"ok": False, "detail": "not_configured"}
        return fetch_mexc_wallet()

    @app.post("/api/bot/pause")
    def api_bot_pause() -> Any:
        if set_bot_paused is None:
            raise HTTPException(status_code=501, detail="Pause not available")
        set_bot_paused(True)
        return {"ok": True, "bot_paused": True}

    @app.post("/api/bot/resume")
    def api_bot_resume() -> Any:
        if set_bot_paused is None:
            raise HTTPException(status_code=501, detail="Resume not available")
        set_bot_paused(False)
        return {"ok": True, "bot_paused": False}

    def _after_save() -> None:
        if on_state_persisted is not None:
            on_state_persisted()

    @app.post("/api/state/reset")
    def api_reset() -> Any:
        st = BotState()
        set_state(st)
        state_store.save(st)
        _after_save()
        return {"ok": True}

    @app.get("/api/state/export")
    def api_state_export() -> Any:
        st = get_state()
        return state_store.to_dict(st)

    @app.post("/api/state/import")
    def api_state_import(
        payload: dict[str, Any] = Body(...),
        merge: bool = Query(False, description="Merge into current state instead of replacing."),
        apply_equity: bool = Query(
            False,
            description="When merge=true, also set equity/start_equity from payload (default: keep current).",
        ),
    ) -> Any:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")

        try:
            if not merge:
                st = state_store.from_dict(payload)
            else:
                cur = get_state()
                base = state_store.to_dict(cur)
                pos_in = payload.get("positions") or {}
                if not isinstance(pos_in, dict):
                    raise HTTPException(status_code=400, detail="positions must be an object")
                merged_pos = dict(base.get("positions") or {})
                for sym, row in pos_in.items():
                    if not isinstance(row, dict):
                        continue
                    merged_pos[sym] = row
                base["positions"] = merged_pos

                trades_in = payload.get("trades") or []
                if not isinstance(trades_in, list):
                    raise HTTPException(status_code=400, detail="trades must be a list")
                seen = {(str(t.get("time")), str(t.get("type")), str(t.get("symbol"))) for t in base.get("trades") or []}
                for t in trades_in:
                    if not isinstance(t, dict):
                        continue
                    key = (str(t.get("time")), str(t.get("type")), str(t.get("symbol")))
                    if key in seen:
                        continue
                    seen.add(key)
                    base.setdefault("trades", []).append(t)

                if apply_equity:
                    if "equity" in payload:
                        base["equity"] = float(payload["equity"])
                    if "start_equity" in payload:
                        base["start_equity"] = float(payload["start_equity"])

                for k in ("cooldown_until", "symbol_block_until", "symbol_loss_streak"):
                    if k in payload and isinstance(payload[k], dict):
                        merged = dict(base.get(k) or {})
                        merged.update(payload[k])
                        base[k] = merged

                # Do not apply payload updated_at — keep server time; save() will set fresh updated_at.
                for k in ("market_regime", "regime_entry_risk", "regime_breadth", "regime_universe"):
                    if k in payload and payload[k] is not None:
                        if k == "regime_entry_risk":
                            base[k] = float(payload[k])
                        elif k in ("regime_breadth", "regime_universe"):
                            base[k] = int(payload[k])
                        else:
                            base[k] = str(payload[k])

                st = state_store.from_dict(base)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid state payload: {exc}") from exc

        set_state(st)
        state_store.save(st)
        _after_save()
        return {"ok": True, "merged": bool(merge), "positions": len(st.positions), "trades": len(st.trades)}

    return app

