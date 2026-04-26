from __future__ import annotations

import requests


class TelegramNotifier:
    def __init__(self, bot_token: str | None, chat_id: str | None, timeout_s: int = 15):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout_s = timeout_s

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            r = requests.post(url, json={"chat_id": self.chat_id, "text": text}, timeout=self.timeout_s)
            r.raise_for_status()
        except Exception:
            # Notifications must never crash trading loop
            return

