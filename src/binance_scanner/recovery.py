from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from binance_scanner.binance.trading import BinanceTradingClient
from binance_scanner.config import Settings
from binance_scanner.models import TradeExecution
from binance_scanner.proposals import record_audit_event


async def reconcile_spot_executions(session: AsyncSession, settings: Settings) -> int:
    if (
        not settings.trading_enabled
        or settings.binance_api_key is None
        or settings.binance_api_secret is None
    ):
        return 0
    result = await session.execute(
        select(TradeExecution).where(
            TradeExecution.status.in_(["pending", "submitted", "reconciliation_required"])
        )
    )
    records = list(result.scalars().all())
    if not records:
        return 0
    recovered = 0
    async with BinanceTradingClient(
        str(settings.binance_rest_base_url),
        settings.binance_api_key.get_secret_value(),
        settings.binance_api_secret.get_secret_value(),
        enabled=True,
        timeout_seconds=settings.binance_timeout_seconds,
    ) as client:
        for record in records:
            try:
                payload = await client.get_order_status(record.client_order_id)
            except Exception:
                continue
            status = str(payload.get("status", "UNKNOWN")).upper()
            if payload.get("orderId") is not None:
                record.exchange_order_id = str(payload["orderId"])
            if status == "FILLED":
                record.status = "filled"
                record.completed_at = datetime.now(UTC)
            elif status in {"NEW", "PARTIALLY_FILLED"}:
                record.status = status.lower()
            elif status in {"CANCELED", "REJECTED", "EXPIRED"}:
                record.status = status.lower()
                record.completed_at = datetime.now(UTC)
            else:
                continue
            record.message = f"reconciled Binance status: {status}"
            recovered += 1
            await record_audit_event(
                session,
                "spot_execution_reconciled",
                "recovery_service",
                {"execution_id": record.id, "status": status},
                str(record.id),
            )
    return recovered
