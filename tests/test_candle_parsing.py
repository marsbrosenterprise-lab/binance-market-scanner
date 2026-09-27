from __future__ import annotations

from decimal import Decimal

from binance_scanner.market_data import parse_rest_kline, parse_ws_kline


def test_parse_rest_kline() -> None:
    candle = parse_rest_kline(
        "BTCUSDT",
        "1m",
        [1700000000000, "100", "105", "99", "103", "12.5", 1700000059999, "1287.5", 42],
    )

    assert candle.symbol == "BTCUSDT"
    assert candle.close_price == Decimal("103")
    assert candle.trade_count == 42
    assert candle.is_closed is True


def test_parse_combined_websocket_kline() -> None:
    candle = parse_ws_kline(
        {
            "stream": "btcusdt@kline_1m",
            "data": {
                "e": "kline",
                "k": {
                    "t": 1700000000000,
                    "T": 1700000059999,
                    "s": "BTCUSDT",
                    "i": "1m",
                    "o": "100",
                    "c": "103",
                    "h": "105",
                    "l": "99",
                    "v": "12.5",
                    "q": "1287.5",
                    "n": 42,
                    "x": False,
                },
            },
        }
    )

    assert candle.symbol == "BTCUSDT"
    assert candle.interval == "1m"
    assert candle.is_closed is False
