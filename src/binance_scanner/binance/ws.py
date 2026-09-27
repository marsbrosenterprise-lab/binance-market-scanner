from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import websockets

logger = logging.getLogger(__name__)


class BinanceMarketStream:
    """Reconnectable public market stream for Binance Spot Testnet."""

    def __init__(self, base_url: str, symbols: list[str], stream: str = "kline_1m") -> None:
        if not symbols:
            raise ValueError("at least one symbol is required")
        self._base_url = base_url.rstrip("/")
        self._symbols = [symbol.lower() for symbol in symbols]
        self._stream = stream

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        delay = 1.0
        while True:
            try:
                async with websockets.connect(self._url(), ping_interval=20, ping_timeout=60) as ws:
                    delay = 1.0
                    async for message in ws:
                        payload = json.loads(message)
                        if isinstance(payload, dict):
                            yield payload
            except asyncio.CancelledError:
                raise
            except (OSError, json.JSONDecodeError, websockets.WebSocketException) as exc:
                logger.warning("market stream disconnected", extra={"event_type": "ws_disconnect"})
                logger.debug("market stream disconnect detail: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    def _url(self) -> str:
        streams = "/".join(f"{symbol}@{self._stream}" for symbol in self._symbols)
        if self._base_url.endswith("/ws"):
            return f"{self._base_url[:-3]}/stream?streams={streams}"
        return f"{self._base_url}/stream?streams={streams}"
