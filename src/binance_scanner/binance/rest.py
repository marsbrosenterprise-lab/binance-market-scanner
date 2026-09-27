from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx


class BinanceRestError(RuntimeError):
    """Raised when Binance returns an invalid or unsuccessful response."""


class BinanceRestClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, transport=transport
        )

    async def __aenter__(self) -> BinanceRestClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.__aexit__(*args)

    async def get_exchange_info(self) -> Mapping[str, Any]:
        payload = await self._get_json("/api/v3/exchangeInfo")
        if not isinstance(payload, Mapping):
            raise BinanceRestError("Binance returned an invalid exchange-info payload")
        return payload

    async def get_klines(
        self, symbol: str, interval: str = "1m", limit: int = 500
    ) -> list[list[Any]]:
        if not symbol or symbol != symbol.upper():
            raise ValueError("symbol must be an uppercase Binance symbol")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        payload = await self._get_json(
            "/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit}
        )
        if not isinstance(payload, list):
            raise BinanceRestError("Binance returned an invalid klines payload")
        return payload

    async def get_ticker_price(self, symbol: str) -> str:
        if not symbol or symbol != symbol.upper():
            raise ValueError("symbol must be an uppercase Binance symbol")
        payload = await self._get_json("/api/v3/ticker/price", params={"symbol": symbol})
        if not isinstance(payload, Mapping) or not isinstance(payload.get("price"), str):
            raise BinanceRestError("Binance returned an invalid ticker payload")
        price = payload.get("price")
        if not isinstance(price, str):
            raise BinanceRestError("Binance returned an invalid ticker price")
        return price

    async def _get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BinanceRestError(f"Binance request failed: {path}") from exc
