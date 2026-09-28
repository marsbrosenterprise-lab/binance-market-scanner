from decimal import Decimal

import httpx
import pytest

from binance_scanner.binance.trading import BinanceTradingClient, sign_params
from binance_scanner.execution import OrderExecutionUncertain, OrderIntent


def test_sign_params_is_deterministic() -> None:
    assert sign_params({"symbol": "BTCUSDT", "timestamp": 1}, b"secret") == (
        "ef9d3d77a34d9a13a21a4c2d7f3e8cb091888a74ca62b5b62f430e78eded95ba"
    )


def test_signed_client_requires_testnet_and_explicit_gate() -> None:
    with pytest.raises(ValueError, match="sandbox endpoints"):
        BinanceTradingClient("https://api.binance.com", "key", "secret", enabled=True)
    with pytest.raises(ValueError, match="enabled=True"):
        BinanceTradingClient("https://testnet.binance.vision", "key", "secret", enabled=False)


@pytest.mark.asyncio
async def test_signed_client_submits_expected_testnet_request() -> None:
    request_seen: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_seen
        request_seen = request
        return httpx.Response(200, json={"orderId": 123, "status": "NEW"})

    intent = OrderIntent("proposal-7", "BTCUSDT", "BUY", Decimal("0.25"), Decimal("100"))
    async with BinanceTradingClient(
        "https://testnet.binance.vision",
        "key",
        "secret",
        enabled=True,
        clock_ms=lambda: 1_700_000_000_000,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.execute(intent)

    assert request_seen is not None
    assert request_seen.headers["X-MBX-APIKEY"] == "key"
    assert request_seen.url.path == "/api/v3/order"
    assert request_seen.url.params["newClientOrderId"] == "proposal-7"
    assert request_seen.url.params["timestamp"] == "1700000000000"
    assert request_seen.url.params["signature"]
    assert result.exchange_order_id == "123"
    assert result.simulated is False


@pytest.mark.asyncio
async def test_signed_client_supports_demo_endpoint() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"orderId": 456, "status": "FILLED"})

    intent = OrderIntent("proposal-8", "ETHUSDT", "BUY", Decimal("0.003"), Decimal("10"))
    async with BinanceTradingClient(
        "https://demo-api.binance.com",
        "key",
        "secret",
        enabled=True,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.execute(intent)

    assert result.exchange_order_id == "456"
    assert result.message == "order submitted to Binance Spot Demo"


@pytest.mark.asyncio
async def test_signed_client_marks_timeout_as_uncertain_without_retrying() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("exchange did not answer")

    intent = OrderIntent("proposal-9", "BTCUSDT", "BUY", Decimal("0.25"), Decimal("100"))
    async with BinanceTradingClient(
        "https://testnet.binance.vision",
        "key",
        "secret",
        enabled=True,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(OrderExecutionUncertain, match="outcome is unknown"):
            await client.execute(intent)

    assert calls == 1
