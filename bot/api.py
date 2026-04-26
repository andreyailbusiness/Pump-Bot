from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .state import BotState, StateStore


def create_app(state_store: StateStore, get_state: Callable[[], BotState]) -> FastAPI:
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
        d["positions"] = {k: asdict(v) for k, v in st.positions.items()}
        return d

    @app.get("/api/positions")
    def api_positions() -> Any:
        st = get_state()
        return {k: asdict(v) for k, v in st.positions.items()}

    @app.get("/api/trades")
    def api_trades(limit: int = 200) -> Any:
        st = get_state()
        return list(reversed(st.trades[-limit:]))

    @app.post("/api/state/reset")
    def api_reset() -> Any:
        st = BotState()
        state_store.save(st)
        return {"ok": True}

    return app

