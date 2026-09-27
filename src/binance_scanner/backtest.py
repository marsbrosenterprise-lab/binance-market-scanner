from __future__ import annotations

from dataclasses import dataclass, replace

from binance_scanner.indicators import CandlePoint
from binance_scanner.risk import RiskEngine
from binance_scanner.strategies import TrendMomentumStrategy


@dataclass(frozen=True, slots=True)
class BacktestCandle:
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC values are inconsistent")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_balance: float = 10_000.0
    fee_fraction: float = 0.001
    slippage_fraction: float = 0.0005
    warmup_candles: int = 30

    def __post_init__(self) -> None:
        if self.initial_balance <= 0:
            raise ValueError("initial_balance must be positive")
        if self.fee_fraction < 0 or self.slippage_fraction < 0:
            raise ValueError("fee and slippage fractions cannot be negative")
        if self.warmup_candles < 1:
            raise ValueError("warmup_candles must be positive")


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    signal_index: int
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    exit_reason: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    initial_balance: float
    ending_balance: float
    total_return_fraction: float
    trades: tuple[BacktestTrade, ...]

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(trade.pnl > 0 for trade in self.trades) / len(self.trades)


@dataclass(frozen=True, slots=True)
class _OpenPosition:
    signal_index: int
    entry_index: int
    entry_price: float
    quantity: float
    stop_price: float
    target_price: float


class BacktestEngine:
    def __init__(
        self,
        strategy: TrendMomentumStrategy | None = None,
        risk_engine: RiskEngine | None = None,
        config: BacktestConfig | None = None,
    ) -> None:
        self.strategy = strategy or TrendMomentumStrategy()
        self.risk_engine = risk_engine or RiskEngine()
        self.config = config or BacktestConfig()

    def run(self, symbol: str, interval: str, candles: list[BacktestCandle]) -> BacktestResult:
        if len(candles) <= self.config.warmup_candles:
            raise ValueError("not enough candles for configured warmup")

        balance = self.config.initial_balance
        trades: list[BacktestTrade] = []
        position: _OpenPosition | None = None
        index = self.config.warmup_candles
        while index < len(candles):
            candle = candles[index]
            if position is not None:
                exit_price, reason = self._exit_price(position, candle)
                if exit_price is None or reason is None:
                    index += 1
                    continue
                pnl = self._close_pnl(position, exit_price)
                balance += pnl
                trades.append(
                    BacktestTrade(
                        signal_index=position.signal_index,
                        entry_index=position.entry_index,
                        exit_index=index,
                        entry_price=position.entry_price,
                        exit_price=exit_price,
                        quantity=position.quantity,
                        pnl=pnl,
                        exit_reason=reason,
                    )
                )
                position = None
                index += 1
                continue

            history = [
                CandlePoint(high=item.high, low=item.low, close=item.close)
                for item in candles[: index + 1]
            ]
            candidate = self.strategy.evaluate(symbol, interval, history)
            if candidate.status != "candidate" or index + 1 >= len(candles):
                index += 1
                continue

            next_open = candles[index + 1].open * (1 + self.config.slippage_fraction)
            adjusted = replace(candidate, entry_price=next_open)
            if (
                candidate.stop_price is None
                or candidate.target_price is None
                or candidate.stop_price >= next_open
                or candidate.target_price <= next_open
            ):
                index += 1
                continue
            risk = self.risk_engine.assess(adjusted, balance)
            if not risk.accepted or risk.quantity is None:
                index += 1
                continue
            position = _OpenPosition(
                signal_index=index,
                entry_index=index + 1,
                entry_price=next_open,
                quantity=risk.quantity,
                stop_price=candidate.stop_price,
                target_price=candidate.target_price,
            )
            index += 1

        if position is not None:
            exit_price = candles[-1].close * (1 - self.config.slippage_fraction)
            pnl = self._close_pnl(position, exit_price)
            balance += pnl
            trades.append(
                BacktestTrade(
                    signal_index=position.signal_index,
                    entry_index=position.entry_index,
                    exit_index=len(candles) - 1,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    quantity=position.quantity,
                    pnl=pnl,
                    exit_reason="end_of_data",
                )
            )
        return BacktestResult(
            initial_balance=self.config.initial_balance,
            ending_balance=balance,
            total_return_fraction=(balance / self.config.initial_balance) - 1,
            trades=tuple(trades),
        )

    def _exit_price(
        self, position: _OpenPosition, candle: BacktestCandle
    ) -> tuple[float, str] | tuple[None, None]:
        if candle.low <= position.stop_price:
            return position.stop_price * (1 - self.config.slippage_fraction), "stop"
        if candle.high >= position.target_price:
            return position.target_price * (1 - self.config.slippage_fraction), "target"
        return None, None

    def _close_pnl(self, position: _OpenPosition, exit_price: float) -> float:
        gross = (exit_price - position.entry_price) * position.quantity
        entry_fee = position.entry_price * position.quantity * self.config.fee_fraction
        exit_fee = exit_price * position.quantity * self.config.fee_fraction
        return gross - entry_fee - exit_fee
