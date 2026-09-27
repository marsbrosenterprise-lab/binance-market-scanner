from __future__ import annotations

import json
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from binance_scanner.execution import OrderExecutionError, OrderIntent
from binance_scanner.risk import RiskAssessment


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    min_quantity: Decimal
    max_quantity: Decimal
    quantity_step: Decimal
    min_notional: Decimal


def parse_symbol_filters(filters_json: str) -> SymbolFilters:
    try:
        raw_filters = json.loads(filters_json)
        filters = {item["filterType"]: item for item in raw_filters if isinstance(item, dict)}
        lot_size = filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE")
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
        if lot_size is None or notional is None:
            raise ValueError("required Binance quantity/notional filters are missing")
        return SymbolFilters(
            min_quantity=Decimal(str(lot_size["minQty"])),
            max_quantity=Decimal(str(lot_size["maxQty"])),
            quantity_step=Decimal(str(lot_size["stepSize"])),
            min_notional=Decimal(str(notional["minNotional"])),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError) as exc:
        raise OrderExecutionError("symbol filters are invalid or unavailable") from exc


def validate_order_intent(intent: OrderIntent, filters_json: str) -> None:
    filters = parse_symbol_filters(filters_json)
    if intent.quantity < filters.min_quantity:
        raise OrderExecutionError("quantity is below Binance minimum quantity")
    if intent.quantity > filters.max_quantity:
        raise OrderExecutionError("quantity exceeds Binance maximum quantity")
    if filters.quantity_step <= 0 or intent.quantity % filters.quantity_step != 0:
        raise OrderExecutionError("quantity does not match Binance step size")
    if intent.quantity * intent.reference_price < filters.min_notional:
        raise OrderExecutionError("order notional is below Binance minimum notional")


def normalize_risk_quantity(
    risk: RiskAssessment,
    entry_price: float | None,
    account_balance: float,
    filters_json: str,
    max_notional: float | None = None,
) -> RiskAssessment:
    if not risk.accepted or risk.quantity is None or entry_price is None:
        return risk
    filters = parse_symbol_filters(filters_json)
    quantity = Decimal(str(risk.quantity))
    normalized = quantity - (quantity % filters.quantity_step)
    if max_notional is not None:
        notional_cap = Decimal(str(max_notional))
        capped_quantity = notional_cap / Decimal(str(entry_price))
        capped_quantity -= capped_quantity % filters.quantity_step
        normalized = min(normalized, capped_quantity)
    notional = normalized * Decimal(str(entry_price))
    if normalized < filters.min_quantity:
        return replace(
            risk, accepted=False, reasons=("quantity is below Binance minimum quantity",)
        )
    if normalized > filters.max_quantity:
        return replace(risk, accepted=False, reasons=("quantity exceeds Binance maximum quantity",))
    if notional < filters.min_notional:
        return replace(
            risk, accepted=False, reasons=("order notional is below Binance minimum notional",)
        )
    estimated_loss = normalized / quantity * Decimal(str(risk.estimated_loss or 0))
    return replace(
        risk,
        quantity=float(normalized),
        notional=float(notional),
        estimated_loss=float(estimated_loss),
        max_loss_fraction=float(estimated_loss / Decimal(str(account_balance))),
    )
