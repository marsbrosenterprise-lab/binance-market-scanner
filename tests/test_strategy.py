from __future__ import annotations

from binance_scanner.indicators import CandlePoint, IndicatorEngine
from binance_scanner.strategies import TrendMomentumStrategy


def _rising_candles(count: int = 50) -> list[CandlePoint]:
    close = 100.0
    candles = []
    for index in range(count):
        close += (1.0, 1.0, -1.0)[index % 3]
        candles.append(CandlePoint(high=close + 1, low=close - 1, close=close))
    return candles


def test_strategy_returns_candidate_when_conditions_are_met() -> None:
    strategy = TrendMomentumStrategy(IndicatorEngine(period=14))
    candidate = strategy.evaluate("BTCUSDT", "1m", _rising_candles())

    assert candidate.status == "candidate"
    assert candidate.side == "BUY"
    assert candidate.stop_price is not None
    assert candidate.target_price is not None
    assert candidate.risk_reward == 1.5


def test_strategy_explains_rejected_conditions() -> None:
    candles = [CandlePoint(high=100, low=98, close=99) for _ in range(50)]
    candidate = TrendMomentumStrategy().evaluate("BTCUSDT", "1m", candles)

    assert candidate.status == "no_signal"
    assert candidate.side is None
    assert candidate.rationale
