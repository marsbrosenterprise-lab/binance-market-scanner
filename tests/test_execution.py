from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from binance_scanner.execution import (
    DryRunOrderExecutor,
    OrderExecutionError,
    order_intent_from_proposal,
)
from binance_scanner.models import TradeProposal


def _proposal(state: str = "approved") -> TradeProposal:
    proposal = TradeProposal(
        id=42,
        symbol="BTCUSDT",
        interval="1m",
        state=state,
        version=2,
        signal_json='{"side":"BUY","entry_price":100.0}',
        risk_json='{"quantity":0.25}',
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    return proposal


def test_only_approved_proposals_create_order_intents() -> None:
    intent = order_intent_from_proposal(_proposal())

    assert intent.client_order_id == "proposal-42"
    assert intent.quantity == Decimal("0.25")
    assert intent.reference_price == Decimal("100.0")

    with pytest.raises(OrderExecutionError, match="only approved"):
        order_intent_from_proposal(_proposal("pending"))


@pytest.mark.asyncio
async def test_dry_run_executor_never_returns_exchange_order_id() -> None:
    result = await DryRunOrderExecutor().execute(order_intent_from_proposal(_proposal()))

    assert result.status == "simulated"
    assert result.simulated is True
    assert result.exchange_order_id is None
