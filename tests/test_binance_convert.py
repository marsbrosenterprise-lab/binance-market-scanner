from decimal import Decimal

import httpx
import pytest

from binance_scanner.binance.convert import BinanceConvertClient


def test_convert_client_requires_live_binance_endpoint() -> None:
    with pytest.raises(ValueError, match="api.binance.com"):
        BinanceConvertClient("https://demo-api.binance.com", "key", "secret", enabled=True)


@pytest.mark.asyncio
async def test_convert_client_quotes_and_accepts_with_signed_requests() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("getQuote"):
            return httpx.Response(
                200,
                json={
                    "quoteId": "quote-1",
                    "fromAsset": "USDT",
                    "toAsset": "ETH",
                    "fromAmount": "10",
                    "toAmount": "0.003",
                    "validTimestamp": 1_800_000_000_000,
                },
            )
        if request.url.path.endswith("acceptQuote"):
            return httpx.Response(200, json={"orderId": "order-1"})
        return httpx.Response(200, json={"orderStatus": "SUCCESS"})

    async with BinanceConvertClient(
        "https://api.binance.com",
        "key",
        "secret",
        enabled=True,
        clock_ms=lambda: 1_700_000_000_000,
        transport=httpx.MockTransport(handler),
    ) as client:
        quote = await client.get_quote("USDT", "ETH", str(Decimal("10")))
        accepted = await client.accept_quote("quote-1")
        status = await client.order_status(order_id=str(accepted["orderId"]))

    assert quote["quoteId"] == "quote-1"
    assert accepted["orderId"] == "order-1"
    assert status["orderStatus"] == "SUCCESS"
    assert paths == [
        "/sapi/v1/convert/getQuote",
        "/sapi/v1/convert/acceptQuote",
        "/sapi/v1/convert/orderStatus",
    ]
