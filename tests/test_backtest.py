import pytest

from binance_scanner.backtest import BacktestCandle, BacktestConfig, BacktestEngine
from binance_scanner.strategies import SignalCandidate, TrendMomentumStrategy


def test_backtest_rejects_lookahead_insufficient_data() -> None:
    engine = BacktestEngine(config=BacktestConfig(warmup_candles=3))

    with pytest.raises(ValueError, match="not enough candles"):
        engine.run("BTCUSDT", "1m", [BacktestCandle(100, 101, 99, 100)] * 3)


def test_backtest_closes_open_position_at_end_of_data() -> None:
    class FixedCandidateStrategy(TrendMomentumStrategy):
        def evaluate(self, symbol: str, interval: str, candles: list) -> SignalCandidate:
            return SignalCandidate(
                symbol=symbol,
                interval=interval,
                strategy_name="test",
                strategy_version="1",
                status="candidate",
                side="BUY",
                confidence=0.8,
                entry_price=candles[-1].close,
                stop_price=candles[-1].close - 14,
                target_price=candles[-1].close + 1,
                risk_reward=1,
                rationale=("test",),
            )

    candles = [BacktestCandle(100, 101, 99, 100) for _ in range(5)]
    candles[3] = BacktestCandle(100, 110, 99, 109)
    candles[4] = BacktestCandle(109, 112, 108, 111)
    engine = BacktestEngine(
        strategy=FixedCandidateStrategy(),
        config=BacktestConfig(warmup_candles=3, slippage_fraction=0),
    )

    result = engine.run("BTCUSDT", "1m", candles)

    assert result.initial_balance == 10_000
    assert result.ending_balance > result.initial_balance
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "target"
    assert result.trades[0].exit_price > result.trades[0].entry_price
