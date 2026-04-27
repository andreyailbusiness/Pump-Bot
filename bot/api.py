from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException
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
    def api_state_import(payload: dict[str, Any] = Body(...)) -> Any:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")

        try:
            st = state_store.from_dict(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid state payload: {exc}") from exc

        set_state(st)
        state_store.save(st)
        return {"ok": True, "positions": len(st.positions), "trades": len(st.trades)}

    return app

