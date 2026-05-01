from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .state import BotState, StateStore


def _position_upnl(side: str, qty: float, entry_price: float, last_price: float | None) -> float | None:
    if last_price is None:
        return None
    if side == "long":
        return (last_price - entry_price) * qty
    return (entry_price - last_price) * qty


def create_app(
    state_store: StateStore,
    get_state: Callable[[], BotState],
    set_state: Callable[[BotState], None],
    max_drawdown_limit: float = 0.08,
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
        out_positions: dict[str, Any] = {}
        for k, v in st.positions.items():
            p = asdict(v)
            p["upnl"] = _position_upnl(
                side=str(v.side),
                qty=float(v.qty),
                entry_price=float(v.entry_price),
                last_price=(float(v.last_price) if v.last_price is not None else None),
            )
            out_positions[k] = p
        d["positions"] = out_positions
        d["max_drawdown_limit"] = float(max_drawdown_limit)
        return d

    @app.get("/api/positions")
    def api_positions() -> Any:
        st = get_state()
        out_positions: dict[str, Any] = {}
        for k, v in st.positions.items():
            p = asdict(v)
            p["upnl"] = _position_upnl(
                side=str(v.side),
                qty=float(v.qty),
                entry_price=float(v.entry_price),
                last_price=(float(v.last_price) if v.last_price is not None else None),
            )
            out_positions[k] = p
        return out_positions

    @app.get("/api/trades")
    def api_trades(limit: int = 200) -> Any:
        st = get_state()
        return list(reversed(st.trades[-limit:]))

    @app.post("/api/state/reset")
    def api_reset() -> Any:
        st = BotState()
        set_state(st)
        state_store.save(st)
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

                for k in ("market_regime", "regime_entry_risk", "regime_breadth", "regime_universe", "updated_at"):
                    if k in payload and payload[k] is not None:
                        if k == "regime_entry_risk":
                            base[k] = float(payload[k])
                        elif k in ("regime_breadth", "regime_universe"):
                            base[k] = int(payload[k])
                        elif k == "updated_at":
                            base[k] = str(payload[k])
                        else:
                            base[k] = str(payload[k])

                st = state_store.from_dict(base)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid state payload: {exc}") from exc

        set_state(st)
        state_store.save(st)
        return {"ok": True, "merged": bool(merge), "positions": len(st.positions), "trades": len(st.trades)}

    return app

