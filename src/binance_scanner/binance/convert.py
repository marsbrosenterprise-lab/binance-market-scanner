from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from decimal import Decimal, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx


class BinanceConvertError(RuntimeError):
    """Raised when Binance rejects a Convert API request."""


class BinanceConvertClient:
    """Signed live Spot Convert adapter with an explicit construction gate."""

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
        if host != "api.binance.com":
            raise ValueError("live Convert is restricted to api.binance.com")
        if not enabled:
            raise ValueError("live Convert client requires an explicit enabled=True gate")
        if not api_key or not api_secret:
            raise ValueError("live Convert API credentials are required")
        if recv_window < 1 or recv_window > 60_000:
            raise ValueError("recv_window must be between 1 and 60000 milliseconds")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._recv_window = recv_window
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, transport=transport
        )

    async def __aenter__(self) -> BinanceConvertClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.__aexit__(*args)

    async def get_quote(
        self, from_asset: str, to_asset: str, from_amount: str
    ) -> Mapping[str, Any]:
        # Binance Convert accepts a maximum of 8 fractional digits for the
        # amount. Decimal values coming from JSON/SQL can carry 18 digits
        # (for example 0.010000000000000000), which Binance rejects even
        # though the mathematical amount is valid.
        normalized_amount = format(
            Decimal(str(from_amount)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN),
            "f",
        )
        return await self._post(
            "/sapi/v1/convert/getQuote",
            {
                "fromAsset": from_asset,
                "toAsset": to_asset,
                "fromAmount": normalized_amount,
                "walletType": "SPOT",
                "validTime": "10s",
            },
        )

    async def accept_quote(self, quote_id: str) -> Mapping[str, Any]:
        return await self._post("/sapi/v1/convert/acceptQuote", {"quoteId": quote_id})

    async def order_status(
        self, *, order_id: str | None = None, quote_id: str | None = None
    ) -> Mapping[str, Any]:
        if not order_id and not quote_id:
            raise ValueError("order_id or quote_id is required")
        params: dict[str, str] = {}
        if order_id:
            params["orderId"] = order_id
        if quote_id:
            params["quoteId"] = quote_id
        return await self._get("/sapi/v1/convert/orderStatus", params)

    async def _post(self, path: str, values: Mapping[str, str]) -> Mapping[str, Any]:
        params = self._signed_params(values)
        try:
            response = await self._client.post(
                path, data=params, headers={"X-MBX-APIKEY": self._api_key}
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise BinanceConvertError(self._exchange_error_message(path, exc.response)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise BinanceConvertError(f"Binance Convert request failed: {path}") from exc
        if not isinstance(payload, Mapping):
            raise BinanceConvertError("Binance returned an invalid Convert response")
        return payload

    async def _get(self, path: str, values: Mapping[str, str]) -> Mapping[str, Any]:
        params = self._signed_params(values)
        try:
            response = await self._client.get(
                path, params=params, headers={"X-MBX-APIKEY": self._api_key}
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise BinanceConvertError(self._exchange_error_message(path, exc.response)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise BinanceConvertError(f"Binance Convert request failed: {path}") from exc
        if not isinstance(payload, Mapping):
            raise BinanceConvertError("Binance returned an invalid Convert response")
        return payload

    @staticmethod
    def _exchange_error_message(path: str, response: httpx.Response) -> str:
        """Return a safe diagnostic without echoing credentials or signatures."""
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, Mapping):
            code = payload.get("code")
            message = payload.get("msg")
            if code is not None or message:
                return f"Binance Convert rejected {path}: code={code}, message={message}"
        return f"Binance Convert rejected {path}: HTTP {response.status_code}"

    def _signed_params(self, values: Mapping[str, str]) -> dict[str, str | int]:
        params: dict[str, str | int] = dict(values)
        params["recvWindow"] = self._recv_window
        params["timestamp"] = self._clock_ms()
        params["signature"] = hmac.new(
            self._api_secret, urlencode(params).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return params
