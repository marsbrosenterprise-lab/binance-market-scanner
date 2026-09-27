from decimal import Decimal

import pytest

from binance_scanner.binance.filters import (
    normalize_risk_quantity,
    parse_symbol_filters,
    validate_order_intent,
)
from binance_scanner.execution import OrderExecutionError, OrderIntent
from binance_scanner.risk import RiskAssessment

FILTERS = (
    '[{"filterType":"LOT_SIZE","minQty":"0.001","maxQty":"100","stepSize":"0.001"},'
    '{"filterType":"MIN_NOTIONAL","minNotional":"10"}]'
)


def test_symbol_filters_validate_quantity_and_notional() -> None:
    filters = parse_symbol_filters(FILTERS)

    assert filters.min_quantity == Decimal("0.001")
    validate_order_intent(
        OrderIntent("proposal-1", "BTCUSDT", "BUY", Decimal("0.01"), Decimal("1000")),
        FILTERS,
    )

    with pytest.raises(OrderExecutionError, match="step size"):
        validate_order_intent(
            OrderIntent("proposal-2", "BTCUSDT", "BUY", Decimal("0.0015"), Decimal("1000")),
            FILTERS,
        )


def test_symbol_filters_reject_small_notional() -> None:
    with pytest.raises(OrderExecutionError, match="minimum notional"):
        validate_order_intent(
            OrderIntent("proposal-3", "BTCUSDT", "BUY", Decimal("0.001"), Decimal("100")),
            FILTERS,
        )


def test_normalize_risk_quantity_rounds_down_to_step() -> None:
    risk = RiskAssessment(True, ("ok",), 0.0118786007, 11.8786, 0.12, 0.000012)

    normalized = normalize_risk_quantity(risk, 1000, 10_000, FILTERS)

    assert normalized.quantity == 0.011
    assert normalized.notional == 11.0


def test_normalize_risk_quantity_applies_absolute_notional_cap() -> None:
    risk = RiskAssessment(True, ("ok",), 1.0, 1000.0, 10.0, 0.001)

    normalized = normalize_risk_quantity(risk, 1000, 10_000, FILTERS, max_notional=10)

    assert normalized.quantity == 0.01
    assert normalized.notional == 10.0
