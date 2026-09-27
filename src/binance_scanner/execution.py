from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

from binance_scanner.models import TradeProposal


class OrderExecutionError(RuntimeError):
    """Raised when an order intent cannot be safely executed."""


@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    reference_price: Decimal

    def __post_init__(self) -> None:
        if not self.client_order_id or len(self.client_order_id) > 36:
            raise ValueError("client_order_id must contain 1 to 36 characters")
        if self.symbol != self.symbol.upper() or not self.symbol.isalnum():
            raise ValueError("symbol must be an uppercase alphanumeric Binance symbol")
        if self.side != "BUY":
            raise ValueError("only BUY intents are supported by the initial execution boundary")
        if self.quantity <= 0 or self.reference_price <= 0:
            raise ValueError("quantity and reference_price must be positive")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: str
    client_order_id: str
    exchange_order_id: str | None
    simulated: bool
    message: str


class OrderExecutor(Protocol):
    async def execute(self, intent: OrderIntent) -> ExecutionResult: ...


class DryRunOrderExecutor:
    """Deterministic executor used until the signed Testnet adapter is approved."""

    async def execute(self, intent: OrderIntent) -> ExecutionResult:
        return ExecutionResult(
            status="simulated",
            client_order_id=intent.client_order_id,
            exchange_order_id=None,
            simulated=True,
            message="order validated but not submitted",
        )


def order_intent_from_proposal(proposal: TradeProposal) -> OrderIntent:
    if proposal.state != "approved":
        raise OrderExecutionError("only approved proposals can become order intents")
    try:
        signal = json.loads(proposal.signal_json)
        risk = json.loads(proposal.risk_json)
        quantity = Decimal(str(risk["quantity"]))
        reference_price = Decimal(str(signal["entry_price"]))
        side = str(signal["side"])
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise OrderExecutionError("proposal contains invalid execution data") from exc
    return OrderIntent(
        client_order_id=f"proposal-{proposal.id}",
        symbol=proposal.symbol,
        side=side,
        quantity=quantity,
        reference_price=reference_price,
    )
