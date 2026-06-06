"""Outbound progress — POST to the notify-relay, which fans out to Telegram.

The relay is the same container devclaw's notify_url callbacks hit. Exact body
shape is confirmed at deploy; we POST {"text": ...} and treat any 2xx as sent.
Notify is best-effort: a relay outage must never crash a tick.
"""

from __future__ import annotations

from typing import Protocol

import httpx


class Notifier(Protocol):
    async def send(self, text: str) -> bool: ...


class HttpNotifier:
    def __init__(self, url: str, timeout_s: float = 10.0) -> None:
        self._url = url
        self._timeout = timeout_s

    async def send(self, text: str) -> bool:
        if not self._url:
            return False
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._url, json={"text": text})
            return resp.is_success
        except Exception:  # noqa: BLE001 — best-effort; never break the tick
            return False


class NullNotifier:
    """No-op notifier (notify disabled / tests)."""

    async def send(self, text: str) -> bool:  # noqa: D401
        return False
