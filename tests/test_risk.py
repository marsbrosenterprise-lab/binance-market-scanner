from __future__ import annotations

from dataclasses import replace

from binance_scanner.risk import RiskConfig, RiskEngine
from binance_scanner.strategies import SignalCandidate


def _candidate() -> SignalCandidate:
    return SignalCandidate(
        symbol="BTCUSDT",
        interval="1m",
        strategy_name="test",
        strategy_version="1.0.0",
        status="candidate",
        side="BUY",
        confidence=0.8,
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        risk_reward=2.0,
        rationale=("test candidate",),
    )


def test_risk_engine_sizes_within_limits() -> None:
    assessment = RiskEngine().assess(_candidate(), account_balance=1000.0)

    assert assessment.accepted is True
    assert assessment.quantity == 1.0
    assert assessment.notional == 100.0
    assert assessment.estimated_loss == 5.0


def test_risk_engine_rejects_excessive_exposure() -> None:
    assessment = RiskEngine(RiskConfig(max_total_exposure_fraction=0.20)).assess(
        _candidate(), account_balance=1000.0, current_exposure=150.0
    )

    assert assessment.accepted is False
    assert "maximum total exposure would be exceeded" in assessment.reasons


def test_risk_engine_rejects_non_signal() -> None:
    candidate = _candidate()
    rejected = replace(candidate, status="no_signal", side=None)

    assessment = RiskEngine().assess(rejected, account_balance=1000.0)

    assert assessment.accepted is False
    assert "signal is not an actionable long candidate" in assessment.reasons
