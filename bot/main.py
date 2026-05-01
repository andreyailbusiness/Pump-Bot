from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Callable

import uvicorn

from .api import create_app
from .config import Settings, get_settings
from .remote_state import choose_newer_state, pull_state_from_github, push_state_to_github
from .runner import apply_live_equity_from_wallet, bot_loop, build_runtime, sync_live_positions
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


async def periodic_github_state_sync(
    state_store: StateStore,
    get_state: Callable[[], BotState],
    settings: Settings,
) -> None:
    interval = max(30, int(settings.github_state_sync_interval_sec))
    while True:
        try:
            payload = state_store.to_dict(get_state())
            await asyncio.to_thread(push_state_to_github, settings, payload)
        except Exception:
            pass
        await asyncio.sleep(interval)


def main() -> None:
    settings = get_settings()
    rt = build_runtime(settings)

    # Single StateStore instance (same as runner); avoids two writers / confusing reloads.
    state_store = rt.state_store

    def get_state() -> BotState:
        return rt.state

    def set_state(st: BotState) -> None:
        rt.state = st
        rt.state_store = state_store

    def push_github_state_now() -> None:
        if not settings.github_state_sync_enabled:
            return
        try:
            push_state_to_github(settings, state_store.to_dict(get_state()))
        except Exception as exc:
            print(f"[state] GitHub push after API save failed: {exc}", flush=True)

    def fetch_mexc_wallet() -> dict:
        if rt.live_exec is None:
            return {"ok": False, "detail": "no_mexc_client"}
        try:
            w = rt.live_exec.fetch_futures_wallet_usdt()
            return {"ok": True, **w}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    def set_bot_paused(val: bool) -> None:
        rt.state.bot_paused = bool(val)
        state_store.save(rt.state)
        push_github_state_now()

    def dashboard_meta() -> dict:
        return {
            "trading_mode": settings.trading_mode,
            "live_enabled": settings.live_enabled,
            "mexc_connected": rt.live_exec is not None,
            "mexc_positions_sync": settings.trading_mode == "live" and settings.mexc_exchange_position_sync,
        }

    app = create_app(
        state_store=state_store,
        get_state=get_state,
        set_state=set_state,
        max_drawdown_limit=float(settings.max_drawdown),
        on_state_persisted=push_github_state_now,
        fetch_mexc_wallet=fetch_mexc_wallet,
        set_bot_paused=set_bot_paused,
        get_dashboard_meta=dashboard_meta,
    )

    @app.on_event("startup")
    async def _startup() -> None:
        if settings.trading_mode != "live":
            print(
                f"[mode] TRADING_MODE={settings.trading_mode!r} — реальные сделки MEXC выключены. "
                "В Render → Environment задайте TRADING_MODE=live и LIVE_ENABLED=true "
                "(ручные переменные перекрывают render.yaml).",
                flush=True,
            )
        elif not settings.live_enabled:
            print(
                "[mode] LIVE_ENABLED=false — ордера на биржу не отправляются. "
                "Поставьте LIVE_ENABLED=true в Render.",
                flush=True,
            )
        if settings.github_state_sync_enabled and not settings.github_state_token:
            print(
                "[state] GITHUB_STATE_SYNC_ENABLED but GITHUB_STATE_TOKEN is unset; "
                "GitHub pull/push are skipped — add the token in Render env to survive deploys.",
                flush=True,
            )
        if settings.github_state_sync_enabled:
            try:
                local_payload = state_store.to_dict(rt.state)
                remote_payload = await asyncio.to_thread(pull_state_from_github, settings)
                selected = choose_newer_state(local_payload, remote_payload)
                rt.state = state_store.from_dict(selected)
                state_store.save(rt.state)
                print(
                    f"[state] restore: positions={len(rt.state.positions)} "
                    f"trades={len(rt.state.trades)} equity={rt.state.equity:.4f} "
                    f"(github={'ok' if remote_payload else 'no-file'})",
                    flush=True,
                )
                await asyncio.to_thread(
                    push_state_to_github, settings, state_store.to_dict(rt.state)
                )
            except Exception as exc:
                print(f"[state] GitHub sync failed (check GITHUB_STATE_TOKEN / repo path): {exc}", flush=True)

        if (
            settings.trading_mode == "live"
            and settings.mexc_exchange_position_sync
            and rt.live_exec is not None
        ):
            try:
                await sync_live_positions(rt)
                state_store.save(rt.state)
                print(
                    f"[mexc] startup sync: open positions={len(rt.state.positions)}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[mexc] startup position sync failed: {exc}", flush=True)

        if settings.trading_mode == "live" and rt.live_exec is not None:
            await asyncio.to_thread(apply_live_equity_from_wallet, rt)
            state_store.save(rt.state)

        # Fresh deploy / missing state file leaves updated_at at 1970; persist once for UI clarity.
        if str(getattr(rt.state, "updated_at", "")).startswith("1970"):
            state_store.save(rt.state)

        asyncio.create_task(bot_loop(rt))
        if settings.state_backup_enabled:
            asyncio.create_task(periodic_state_backup(state_store, get_state, settings))
        if settings.github_state_sync_enabled:
            asyncio.create_task(periodic_github_state_sync(state_store, get_state, settings))

    port = settings.port or settings.api_port
    uvicorn.run(app, host=settings.api_host, port=port, log_level="info")


if __name__ == "__main__":
    main()

