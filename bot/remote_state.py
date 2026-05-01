from __future__ import annotations

import base64
import copy
import json
from datetime import datetime
from typing import Any

import requests

from .config import Settings


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _headers(settings: Settings) -> dict[str, str]:
    if not settings.github_state_token:
        raise RuntimeError("GITHUB_STATE_TOKEN is not set")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {settings.github_state_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _contents_url(settings: Settings) -> str:
    return f"https://api.github.com/repos/{settings.github_state_repo}/contents/{settings.github_state_file_path}"


def pull_state_from_github(settings: Settings) -> dict[str, Any] | None:
    if not settings.github_state_sync_enabled:
        return None
    if not settings.github_state_repo or not settings.github_state_file_path:
        return None
    if not settings.github_state_token:
        return None

    resp = requests.get(
        _contents_url(settings),
        headers=_headers(settings),
        params={"ref": settings.github_state_branch},
        timeout=max(5, int(settings.github_state_timeout_sec)),
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    payload = resp.json()
    raw_b64 = str(payload.get("content", "")).replace("\n", "")
    if not raw_b64:
        return None
    decoded = base64.b64decode(raw_b64.encode("utf-8")).decode("utf-8")
    return json.loads(decoded)


def push_state_to_github(settings: Settings, state_payload: dict[str, Any]) -> None:
    if not settings.github_state_sync_enabled:
        return
    if not settings.github_state_repo or not settings.github_state_file_path:
        return
    if not settings.github_state_token:
        return

    timeout = max(5, int(settings.github_state_timeout_sec))
    headers = _headers(settings)
    url = _contents_url(settings)
    sha: str | None = None

    existing = requests.get(url, headers=headers, params={"ref": settings.github_state_branch}, timeout=timeout)
    if existing.status_code == 200:
        sha = str(existing.json().get("sha", "")) or None
    elif existing.status_code != 404:
        existing.raise_for_status()

    content = json.dumps(state_payload, ensure_ascii=False, indent=2).encode("utf-8")
    body: dict[str, Any] = {
        "message": settings.github_state_commit_message,
        "content": base64.b64encode(content).decode("utf-8"),
        "branch": settings.github_state_branch,
    }
    if sha:
        body["sha"] = sha

    put_resp = requests.put(url, headers=headers, json=body, timeout=timeout)
    put_resp.raise_for_status()


def _is_default_empty_local(payload: dict[str, Any]) -> bool:
    """
    True when local state is still the default template (no activity).
    On a fresh deploy there is no state file: we must not treat it as newer than GitHub.
    """
    positions = payload.get("positions") or {}
    trades = payload.get("trades") or []
    if positions or trades:
        return False
    eq = float(payload.get("equity", 1000.0))
    se = float(payload.get("start_equity", 1000.0))
    return abs(eq - se) < 1e-9


def _trade_key(t: Any) -> tuple[str, str, str]:
    if not isinstance(t, dict):
        return ("", "", "")
    return (str(t.get("time")), str(t.get("type")), str(t.get("symbol")))


def _union_supplement(newer: dict[str, Any], older: dict[str, Any]) -> dict[str, Any]:
    """
    Prefer fields from ``newer`` (by updated_at); add open positions and trades from ``older``
    that are missing. Avoids losing symbols when GitHub has a newer timestamp but an incomplete
    snapshot (e.g. failed push after merge import, then deploy).
    """
    out = copy.deepcopy(newer)
    np = dict(out.get("positions") or {})
    op = older.get("positions") or {}
    for sym, row in op.items():
        if sym not in np and isinstance(row, dict):
            np[sym] = copy.deepcopy(row)
    out["positions"] = np

    trades_out = list(out.get("trades") or [])
    seen = {_trade_key(t) for t in trades_out}
    for t in older.get("trades") or []:
        if not isinstance(t, dict):
            continue
        k = _trade_key(t)
        if k not in seen:
            seen.add(k)
            trades_out.append(copy.deepcopy(t))
    out["trades"] = trades_out
    return out


def choose_newer_state(local_payload: dict[str, Any], remote_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not remote_payload:
        return local_payload
    if _is_default_empty_local(local_payload):
        return remote_payload

    local_dt = _parse_iso(str(local_payload.get("updated_at") or ""))
    remote_dt = _parse_iso(str(remote_payload.get("updated_at") or ""))
    if local_dt is None and remote_dt is None:
        return _union_supplement(local_payload, remote_payload)
    if local_dt is None:
        return _union_supplement(remote_payload, local_payload)
    if remote_dt is None:
        return _union_supplement(local_payload, remote_payload)
    if remote_dt > local_dt:
        return _union_supplement(remote_payload, local_payload)
    return _union_supplement(local_payload, remote_payload)
