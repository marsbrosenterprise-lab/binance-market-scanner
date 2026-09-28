from __future__ import annotations

import httpx
import pytest

from binance_scanner.binance.rest import BinanceRestClient
from binance_scanner.market_data import parse_exchange_symbols


def test_parse_exchange_symbols_filters_quote_asset() -> None:
    symbols = parse_exchange_symbols(
        {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "isSpotTradingAllowed": True,
                    "filters": [],
                },
                {
                    "symbol": "ETHBTC",
                    "baseAsset": "ETH",
                    "quoteAsset": "BTC",
                    "status": "TRADING",
                    "isSpotTradingAllowed": True,
                    "filters": [],
                },
            ]
        }
    )

    assert [symbol.symbol for symbol in symbols] == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_ticker_price_reads_public_price_without_credentials() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/ticker/price"
        assert request.url.params["symbol"] == "XRPUSDT"
        return httpx.Response(200, json={"symbol": "XRPUSDT", "price": "1.5247"})

    async with BinanceRestClient(
        "https://api.binance.com", transport=httpx.MockTransport(handler)
    ) as client:
        assert await client.get_ticker_price("XRPUSDT") == "1.5247"
