from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Callable

import uvicorn

from .api import create_app
from .config import Settings, get_settings
from .runner import bot_loop, build_runtime
from .state import BotState, StateStore


async def periodic_state_backup(
    state_store: StateStore,
    get_state: Callable[[], BotState],
    settings: Settings,
) -> None:
    interval = max(30, int(settings.state_backup_interval_sec))
    keep = max(1, int(settings.state_backup_keep))
    backup_dir = settings.state_backup_dir
    os.makedirs(backup_dir, exist_ok=True)

    while True:
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = os.path.join(backup_dir, f"state_{ts}.json")
            payload = state_store.to_dict(get_state())
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            backups = sorted(
                [os.path.join(backup_dir, name) for name in os.listdir(backup_dir) if name.endswith(".json")]
            )
            if len(backups) > keep:
                for old_path in backups[: len(backups) - keep]:
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
        except Exception:
            pass

        await asyncio.sleep(interval)


def main() -> None:
    settings = get_settings()
    rt = build_runtime(settings)

    # Keep state in-memory for API reads; persist after each loop step.
    state_store = StateStore(path=settings.state_path)

    def get_state() -> BotState:
        return rt.state

    def set_state(st: BotState) -> None:
        rt.state = st
        rt.state_store = state_store

    app = create_app(state_store=state_store, get_state=get_state, set_state=set_state)

    @app.on_event("startup")
    async def _startup() -> None:
        asyncio.create_task(bot_loop(rt))
        if settings.state_backup_enabled:
            asyncio.create_task(periodic_state_backup(state_store, get_state, settings))

    port = settings.port or settings.api_port
    uvicorn.run(app, host=settings.api_host, port=port, log_level="info")


if __name__ == "__main__":
    main()

