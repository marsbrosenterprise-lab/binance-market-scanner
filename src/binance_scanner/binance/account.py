from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import httpx

from binance_scanner.binance.trading import sign_params


class BinanceAccountError(RuntimeError):
    """Raised when a private Binance account request fails."""


class BinanceAccountClient:
    """Read-only signed Spot Testnet account client."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        *,
        timeout_seconds: float = 10.0,
        recv_window: int = 5_000,
        allow_live: bool = False,
        clock_ms: Callable[[], int] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        host = (urlparse(base_url).hostname or "").lower()
        if host == "api.binance.com" and not allow_live:
            raise ValueError("live account access requires an explicit allow_live=True gate")
        if host not in {"testnet.binance.vision", "demo-api.binance.com", "api.binance.com"}:
            raise ValueError("private account access is restricted to approved Binance endpoints")
        if not api_key or not api_secret:
            raise ValueError("Binance API credentials are required for account access")
        if recv_window < 1 or recv_window > 60_000:
            raise ValueError("recv_window must be between 1 and 60000 milliseconds")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._recv_window = recv_window
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, transport=transport
        )

    async def __aenter__(self) -> BinanceAccountClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.__aexit__(*args)

    async def get_account(self) -> Mapping[str, Any]:
        params: dict[str, str | int] = {
            "recvWindow": self._recv_window,
            "timestamp": self._clock_ms(),
        }
        params["signature"] = sign_params(params, self._api_secret)
        try:
            response = await self._client.get(
                "/api/v3/account", params=params, headers={"X-MBX-APIKEY": self._api_key}
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BinanceAccountError("Binance account request failed") from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("balances"), list):
            raise BinanceAccountError("Binance returned an invalid account response")
        return payload


def non_zero_balances(account: Mapping[str, Any]) -> list[dict[str, str]]:
    balances = account.get("balances")
    if not isinstance(balances, list):
        raise BinanceAccountError("account response does not contain balances")
    result: list[dict[str, str]] = []
    for balance in balances:
        if not isinstance(balance, Mapping):
            continue
        asset = balance.get("asset")
        free = balance.get("free")
        locked = balance.get("locked")
        if not isinstance(asset, str) or not isinstance(free, str) or not isinstance(locked, str):
            continue
        try:
            has_free = Decimal(free) > 0
            has_locked = Decimal(locked) > 0
        except InvalidOperation:
            continue
        if has_free or has_locked:
            result.append({"asset": asset, "free": free, "locked": locked})
    return result
