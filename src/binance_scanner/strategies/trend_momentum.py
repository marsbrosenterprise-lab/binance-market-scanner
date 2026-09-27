from __future__ import annotations

from dataclasses import dataclass

from binance_scanner.indicators import CandlePoint, IndicatorEngine


@dataclass(frozen=True, slots=True)
class SignalCandidate:
    symbol: str
    interval: str
    strategy_name: str
    strategy_version: str
    status: str
    side: str | None
    confidence: float
    entry_price: float | None
    stop_price: float | None
    target_price: float | None
    risk_reward: float | None
    rationale: tuple[str, ...]


class TrendMomentumStrategy:
    name = "trend_momentum"
    version = "1.0.0"

    def __init__(self, indicator_engine: IndicatorEngine | None = None) -> None:
        self._indicators = indicator_engine or IndicatorEngine(period=14)

    def evaluate(self, symbol: str, interval: str, candles: list[CandlePoint]) -> SignalCandidate:
        snapshot = self._indicators.compute(candles).values
        close = candles[-1].close if candles else None
        ema = snapshot["ema"]
        rsi = snapshot["rsi"]
        atr = snapshot["atr"]
        rationale: list[str] = []

        if close is None or ema is None or rsi is None or atr is None:
            return self._no_signal(symbol, interval, ("insufficient indicator history",))
        if close <= ema:
            rationale.append("close is not above EMA")
        if not 50 <= rsi <= 70:
            rationale.append("RSI is outside the preferred momentum range")
        if atr <= 0:
            rationale.append("ATR is not positive")
        if rationale:
            return self._no_signal(symbol, interval, tuple(rationale))

        stop_price = close - (2 * atr)
        target_price = close + (3 * atr)
        confidence = min(0.95, 0.55 + ((rsi - 50) / 100))
        return SignalCandidate(
            symbol=symbol,
            interval=interval,
            strategy_name=self.name,
            strategy_version=self.version,
            status="candidate",
            side="BUY",
            confidence=round(confidence, 4),
            entry_price=close,
            stop_price=stop_price,
            target_price=target_price,
            risk_reward=1.5,
            rationale=("close is above EMA", "RSI confirms positive momentum", "ATR is positive"),
        )

    def _no_signal(self, symbol: str, interval: str, rationale: tuple[str, ...]) -> SignalCandidate:
        return SignalCandidate(
            symbol=symbol,
            interval=interval,
            strategy_name=self.name,
            strategy_version=self.version,
            status="no_signal",
            side=None,
            confidence=0.0,
            entry_price=None,
            stop_price=None,
            target_price=None,
            risk_reward=None,
            rationale=rationale,
        )
