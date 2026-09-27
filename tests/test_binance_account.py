import httpx
import pytest

from binance_scanner.binance.account import BinanceAccountClient, non_zero_balances


def test_non_zero_balances_filters_empty_assets() -> None:
    balances = non_zero_balances(
        {
            "balances": [
                {"asset": "USDT", "free": "100.00000000", "locked": "0.00000000"},
                {"asset": "BTC", "free": "0.00000000", "locked": "0.00000000"},
            ]
        }
    )

    assert balances == [{"asset": "USDT", "free": "100.00000000", "locked": "0.00000000"}]


@pytest.mark.asyncio
async def test_account_client_uses_signed_read_only_request() -> None:
    request_seen: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_seen
        request_seen = request
        return httpx.Response(
            200,
            json={
                "canTrade": False,
                "balances": [{"asset": "USDT", "free": "10", "locked": "0"}],
            },
        )

    async with BinanceAccountClient(
        "https://testnet.binance.vision",
        "key",
        "secret",
        clock_ms=lambda: 1_700_000_000_000,
        transport=httpx.MockTransport(handler),
    ) as client:
        account = await client.get_account()

    assert request_seen is not None
    assert request_seen.method == "GET"
    assert request_seen.url.path == "/api/v3/account"
    assert request_seen.headers["X-MBX-APIKEY"] == "key"
    assert request_seen.url.params["timestamp"] == "1700000000000"
    assert request_seen.url.params["signature"]
    assert account["canTrade"] is False


@pytest.mark.asyncio
async def test_account_client_supports_demo_endpoint() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"canTrade": True, "balances": []})

    async with BinanceAccountClient(
        "https://demo-api.binance.com",
        "key",
        "secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        account = await client.get_account()

    assert account["canTrade"] is True
