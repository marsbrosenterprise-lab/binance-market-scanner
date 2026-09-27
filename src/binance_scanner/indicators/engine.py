from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import sqrt


@dataclass(frozen=True, slots=True)
class CandlePoint:
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    values: dict[str, float | None]


IndicatorFunction = Callable[[list[CandlePoint]], float | None]


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period or period < 1:
        return None
    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = (value - current) * multiplier + current
    return current


def _rsi(candles: list[CandlePoint], period: int = 14) -> float | None:
    closes = [candle.close for candle in candles]
    if len(closes) <= period or period < 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(closes)):
        previous = closes[index - 1]
        current = closes[index]
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
    if average_loss == 0:
        return 100.0
    return 100 - (100 / (1 + average_gain / average_loss))


def _atr(candles: list[CandlePoint], period: int = 14) -> float | None:
    if len(candles) <= period or period < 1:
        return None
    true_ranges = []
    previous_close = candles[0].close
    for candle in candles[1:]:
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
        previous_close = candle.close
    if len(true_ranges) < period:
        return None
    current = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        current = (current * (period - 1) + true_range) / period
    return current


def _bollinger_width(candles: list[CandlePoint], period: int = 20) -> float | None:
    closes = [candle.close for candle in candles]
    if len(closes) < period or period < 1:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    standard_deviation = sqrt(sum((value - mean) ** 2 for value in window) / period)
    if mean == 0:
        return None
    return (4 * standard_deviation) / mean


class IndicatorEngine:
    """Registry-backed indicator engine shared by live and backtest paths."""

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("indicator period must be positive")
        self._calculators: dict[str, IndicatorFunction] = {
            "ema": lambda candles: _ema([candle.close for candle in candles], period),
            "rsi": lambda candles: _rsi(candles, period),
            "atr": lambda candles: _atr(candles, period),
            "bollinger_width": lambda candles: _bollinger_width(candles, max(period, 20)),
        }

    def register(self, name: str, calculator: IndicatorFunction) -> None:
        if not name or not name.strip():
            raise ValueError("indicator name must not be empty")
        self._calculators[name] = calculator

    def compute(self, candles: list[CandlePoint]) -> IndicatorSnapshot:
        return IndicatorSnapshot(
            values={name: calculator(candles) for name, calculator in self._calculators.items()}
        )
