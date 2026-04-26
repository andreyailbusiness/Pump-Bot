from __future__ import annotations

import asyncio

import uvicorn

from .api import create_app
from .config import get_settings
from .runner import bot_loop, build_runtime
from .state import BotState, StateStore


def main() -> None:
    settings = get_settings()
    rt = build_runtime(settings)

    # Keep state in-memory for API reads; persist after each loop step.
    state_store = StateStore(path=settings.state_path)

    def get_state() -> BotState:
        return rt.state

    app = create_app(state_store=state_store, get_state=get_state)

    @app.on_event("startup")
    async def _startup() -> None:
        asyncio.create_task(bot_loop(rt))

    port = settings.port or settings.api_port
    uvicorn.run(app, host=settings.api_host, port=port, log_level="info")


if __name__ == "__main__":
    main()

