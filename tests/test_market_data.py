from __future__ import annotations

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
