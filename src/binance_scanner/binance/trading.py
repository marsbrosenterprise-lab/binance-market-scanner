from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from binance_scanner.execution import (
    ExecutionResult,
    OrderExecutionError,
    OrderExecutionUncertain,
    OrderExecutor,
    OrderIntent,
)


class BinanceTradingClient(OrderExecutor):
    """Signed Binance Spot Testnet adapter; construction is explicitly opt-in."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        *,
        enabled: bool,
        timeout_seconds: float = 10.0,
        recv_window: int = 5_000,
        clock_ms: Callable[[], int] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        host = (urlparse(base_url).hostname or "").lower()
        if host not in {"testnet.binance.vision", "demo-api.binance.com"}:
            raise ValueError("signed trading is restricted to Binance sandbox endpoints")
        self._host = host
        if not enabled:
            raise ValueError("signed trading client requires an explicit enabled=True gate")
        if not api_key or not api_secret:
            raise ValueError("Binance API credentials are required for signed trading")
        if recv_window < 1 or recv_window > 60_000:
            raise ValueError("recv_window must be between 1 and 60000 milliseconds")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._recv_window = recv_window
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, transport=transport
        )

    async def __aenter__(self) -> BinanceTradingClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.__aexit__(*args)

    async def execute(self, intent: OrderIntent) -> ExecutionResult:
        if intent.side != "BUY":
            raise OrderExecutionError("signed adapter only accepts BUY intents")
        params: dict[str, str | int] = {
            "symbol": intent.symbol,
            "side": intent.side,
            "type": "MARKET",
            "quantity": _decimal_text(intent.quantity),
            "newClientOrderId": intent.client_order_id,
            "recvWindow": self._recv_window,
            "timestamp": self._clock_ms(),
        }
        params["signature"] = sign_params(params, self._api_secret)
        try:
            response = await self._client.post(
                "/api/v3/order", params=params, headers={"X-MBX-APIKEY": self._api_key}
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise OrderExecutionUncertain(
                "signed Binance order request timed out; exchange outcome is unknown"
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                raise OrderExecutionUncertain(
                    "Binance returned a server error; exchange outcome is unknown"
                ) from exc
            raise OrderExecutionError("signed Binance order request was rejected") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise OrderExecutionError("signed Binance order request failed") from exc
        if not isinstance(payload, Mapping) or "orderId" not in payload:
            raise OrderExecutionError("Binance returned an invalid order response")
        order_id = str(payload["orderId"])
        status = str(payload.get("status", "UNKNOWN"))
        return ExecutionResult(
            status=status.lower(),
            client_order_id=intent.client_order_id,
            exchange_order_id=order_id,
            simulated=False,
            message=(
                "order submitted to Binance Spot "
                + ("Demo" if self._host == "demo-api.binance.com" else "Testnet")
            ),
        )


def sign_params(params: Mapping[str, str | int], secret: bytes) -> str:
    query = urlencode(params)
    return hmac.new(secret, query.encode("utf-8"), hashlib.sha256).hexdigest()


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
