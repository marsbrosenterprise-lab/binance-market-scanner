from __future__ import annotations

from binance_scanner.indicators import CandlePoint, IndicatorEngine


def _candles(count: int = 40) -> list[CandlePoint]:
    return [
        CandlePoint(high=100 + index + 1, low=100 + index - 1, close=100 + index)
        for index in range(count)
    ]


def test_indicator_engine_computes_trend_metrics() -> None:
    snapshot = IndicatorEngine(period=14).compute(_candles())

    assert snapshot.values["ema"] is not None
    assert snapshot.values["rsi"] == 100.0
    assert snapshot.values["atr"] is not None
    assert snapshot.values["bollinger_width"] is not None


def test_indicator_engine_returns_none_with_insufficient_history() -> None:
    snapshot = IndicatorEngine(period=14).compute(_candles(5))

    assert snapshot.values["ema"] is None
    assert snapshot.values["rsi"] is None
    assert snapshot.values["atr"] is None
