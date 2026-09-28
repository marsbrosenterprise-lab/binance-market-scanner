from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from binance_scanner.binance.convert import BinanceConvertClient, BinanceConvertError
from binance_scanner.binance.rest import BinanceRestClient, BinanceRestError
from binance_scanner.config import get_settings
from binance_scanner.database import create_session_factory, dispose_engines
from binance_scanner.indicators import CandlePoint
from binance_scanner.logging import configure_logging
from binance_scanner.models import Candle, ConvertRequest
from binance_scanner.proposals import record_audit_event
from binance_scanner.safety import execution_is_stopped
from binance_scanner.strategies import SignalCandidate, TrendMomentumStrategy

logger = logging.getLogger(__name__)


def candidate_payload(candidate: SignalCandidate) -> dict[str, object]:
    return {
        "symbol": candidate.symbol,
        "interval": candidate.interval,
        "status": candidate.status,
        "side": candidate.side,
        "confidence": candidate.confidence,
        "entry_price": candidate.entry_price,
        "stop_price": candidate.stop_price,
        "target_price": candidate.target_price,
        "rationale": candidate.rationale,
    }


async def scan_configured_symbols(
    session: AsyncSession, symbols: Iterable[str], interval: str, candle_limit: int = 200
) -> list[SignalCandidate]:
    strategy = TrendMomentumStrategy()
    candidates: list[SignalCandidate] = []
    for symbol in symbols:
        result = await session.execute(
            select(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.interval == interval,
                Candle.environment == "sandbox",
                Candle.is_closed.is_(True),
            )
            .order_by(desc(Candle.open_time))
            .limit(candle_limit)
        )
        rows = list(reversed(result.scalars().all()))
        candles = [
            CandlePoint(
                high=float(row.high_price), low=float(row.low_price), close=float(row.close_price)
            )
            for row in rows
        ]
        candidates.append(strategy.evaluate(symbol, interval, candles))
    return candidates


async def evaluate_convert_limits(session: AsyncSession, *, symbol: str, price: Decimal) -> int:
    """Trigger matching plans and execute only explicitly armed plans.

    Unarmed plans only transition to ``triggered``. Armed plans request a fresh
    Binance quote, verify that its effective rate still satisfies the trigger,
    and accept it once. This is deliberately one-shot and never re-arms a plan.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        select(ConvertRequest).where(
            ConvertRequest.state == "watching", ConvertRequest.limit_price.is_not(None)
        )
    )
    triggered = 0
    for plan in result.scalars().all():
        limit_price = plan.limit_price
        if limit_price is None:
            continue
        if plan.expires_at is not None and plan.expires_at <= now:
            plan.state = "expired"
            plan.version += 1
            await record_audit_event(
                session,
                "convert_limit_expired",
                "convert_monitor",
                {"request_id": plan.id},
                str(plan.id),
            )
            continue
        # Avoid requesting quotes on every one-second tick while an armed plan
        # is waiting for a quote that satisfies its limit.
        if (
            plan.auto_execute
            and plan.triggered_at is not None
            and (now - plan.triggered_at).total_seconds() < 5
        ):
            continue
        if plan.from_asset == "USDT" and plan.to_asset == "XRP":
            matches = symbol == "XRPUSDT" and price <= limit_price
        elif plan.from_asset == "XRP" and plan.to_asset == "USDT":
            matches = symbol == "XRPUSDT" and price >= limit_price
        else:
            matches = False
        if not matches:
            continue
        triggered += 1
        trigger_details = {
            "request_id": plan.id,
            "symbol": symbol,
            "market_price": str(price),
            "limit_price": str(limit_price),
            "trigger_direction": plan.trigger_direction,
        }
        settings = get_settings()
        if (
            not plan.auto_execute
            or not settings.live_convert_enabled
            or not settings.live_convert_auto_execution_enabled
        ):
            plan.state = "triggered"
            plan.version += 1
            plan.triggered_at = now
            await record_audit_event(
                session, "convert_limit_triggered", "convert_monitor", trigger_details, str(plan.id)
            )
            continue

        if await execution_is_stopped(session):
            await record_audit_event(
                session,
                "convert_auto_execution_blocked",
                "convert_monitor",
                {**trigger_details, "error": "emergency stop is enabled"},
                str(plan.id),
            )
            continue

        # Auto execution is opt-in per plan. Claim the row in its own committed
        # transaction before any exchange request so a second monitor cannot
        # submit the same plan. The claim is not an exactly-once guarantee for
        # the exchange call; uncertain calls are reconciled by order status.
        key = settings.live_convert_api_key
        secret = settings.live_convert_api_secret
        if key is None or secret is None:
            plan.state = "failed"
            plan.version += 1
            await record_audit_event(
                session,
                "convert_auto_execution_blocked",
                "convert_monitor",
                {**trigger_details, "error": "live Convert credentials are not configured"},
                str(plan.id),
            )
            continue
        claim_id = str(uuid4())
        claim_result = await session.execute(
            select(ConvertRequest)
            .where(
                ConvertRequest.id == plan.id,
                ConvertRequest.state == "watching",
                ConvertRequest.auto_execute.is_(True),
            )
            .with_for_update()
        )
        claimed_plan = claim_result.scalar_one_or_none()
        if claimed_plan is None:
            continue
        plan = claimed_plan
        plan.state = "executing"
        plan.execution_claim_id = claim_id
        plan.execution_claimed_at = now
        plan.execution_intent_json = json.dumps(
            {
                "from_asset": plan.from_asset,
                "to_asset": plan.to_asset,
                "from_amount": str(plan.from_amount),
                "limit_price": str(limit_price),
                "claim_id": claim_id,
            },
            separators=(",", ":"),
        )
        plan.version += 1
        await session.commit()
        accepted_started = False
        quote_id: str | None = None
        try:
            async with BinanceConvertClient(
                str(settings.live_convert_rest_base_url),
                key.get_secret_value(),
                secret.get_secret_value(),
                enabled=True,
                timeout_seconds=settings.binance_timeout_seconds,
            ) as client:
                quote = await client.get_quote(
                    plan.from_asset, plan.to_asset, str(plan.from_amount)
                )
                quote_id_value = quote.get("quoteId")
                from_amount = Decimal(str(quote.get("fromAmount", plan.from_amount)))
                to_amount = Decimal(str(quote.get("toAmount", "0")))
                if not isinstance(quote_id_value, str) or not quote_id_value or to_amount <= 0:
                    raise RuntimeError("fresh Binance quote was incomplete")
                quote_id = quote_id_value
                effective_rate = (
                    from_amount / to_amount
                    if plan.from_asset == "USDT"
                    else to_amount / from_amount
                )
                acceptable = (
                    effective_rate <= limit_price
                    if plan.from_asset == "USDT"
                    else effective_rate >= limit_price
                )
                if not acceptable:
                    plan.state = "watching"
                    plan.version += 1
                    plan.triggered_at = now
                    await record_audit_event(
                        session,
                        "convert_auto_execution_waiting_price",
                        "convert_monitor",
                        {**trigger_details, "effective_rate": str(effective_rate)},
                        str(plan.id),
                    )
                    continue
                plan.quote_id = quote_id
                plan.quote_json = json.dumps(dict(quote), separators=(",", ":"))
                plan.execution_intent_json = json.dumps(
                    {**json.loads(plan.execution_intent_json), "quote_id": quote_id},
                    separators=(",", ":"),
                )
                await session.commit()
                accepted_started = True
                accepted = await client.accept_quote(quote_id)
                order_id = accepted.get("orderId")
                plan.order_id = str(order_id) if order_id is not None else None
                await session.commit()
                status_payload = await client.wait_for_order_status(
                    order_id=str(order_id) if order_id is not None else None,
                    quote_id=quote_id,
                )
            plan.order_status = str(status_payload.get("orderStatus", "UNKNOWN"))
            if plan.order_status == "SUCCESS":
                plan.state = "completed"
            elif plan.order_status in {"PROCESS", "PENDING", "ACCEPT_SUCCESS"}:
                plan.state = "processing"
                plan.reconciliation_required = True
            elif plan.order_status in {"FAIL", "FAILED", "CANCELED", "EXPIRED"}:
                plan.state = "failed"
            else:
                plan.state = "reconciliation_required"
                plan.reconciliation_required = True
            plan.version += 1
            plan.triggered_at = now
            plan.completed_at = now if plan.state in {"completed", "failed"} else None
            await record_audit_event(
                session,
                "convert_auto_execution_completed"
                if plan.state == "completed"
                else "convert_auto_execution_pending",
                "convert_monitor",
                {**trigger_details, "order_id": plan.order_id, "order_status": plan.order_status},
                str(plan.id),
            )
        except (BinanceConvertError, RuntimeError, ValueError) as exc:
            # Once acceptance has started, the outcome is unknown until a
            # status query succeeds. Never submit the quote again automatically.
            plan.state = "reconciliation_required" if accepted_started else "watching"
            plan.reconciliation_required = accepted_started
            plan.version += 1
            plan.triggered_at = now
            await record_audit_event(
                session,
                "convert_auto_execution_failed",
                "convert_monitor",
                {**trigger_details, "error": str(exc)},
                str(plan.id),
            )
    return triggered


async def reconcile_convert_executions(session: AsyncSession) -> int:
    """Recover accepted/uncertain Convert calls without ever resubmitting them."""
    settings = get_settings()
    key = settings.live_convert_api_key
    secret = settings.live_convert_api_secret
    if not settings.live_convert_enabled or key is None or secret is None:
        return 0
    result = await session.execute(
        select(ConvertRequest).where(
            ConvertRequest.state.in_(["executing", "processing", "reconciliation_required"]),
            ConvertRequest.quote_id.is_not(None),
        )
    )
    plans = list(result.scalars().all())
    if not plans:
        return 0
    recovered = 0
    async with BinanceConvertClient(
        str(settings.live_convert_rest_base_url),
        key.get_secret_value(),
        secret.get_secret_value(),
        enabled=True,
        timeout_seconds=settings.binance_timeout_seconds,
    ) as client:
        for plan in plans:
            try:
                status_payload = await client.order_status(
                    order_id=plan.order_id, quote_id=plan.quote_id
                )
            except (BinanceConvertError, RuntimeError, ValueError):
                continue
            status_value = str(status_payload.get("orderStatus", "UNKNOWN"))
            plan.order_status = status_value
            if status_value == "SUCCESS":
                plan.state = "completed"
                plan.completed_at = datetime.now(UTC)
                plan.reconciliation_required = False
            elif status_value in {"PROCESS", "PENDING", "ACCEPT_SUCCESS"}:
                plan.state = "processing"
                plan.reconciliation_required = True
            elif status_value in {"FAIL", "FAILED", "CANCELED", "EXPIRED"}:
                plan.state = "failed"
                plan.reconciliation_required = False
            else:
                plan.state = "reconciliation_required"
                plan.reconciliation_required = True
            plan.version += 1
            recovered += 1
            await record_audit_event(
                session,
                "convert_execution_reconciled",
                "convert_monitor",
                {"request_id": plan.id, "order_status": status_value},
                str(plan.id),
            )
    return recovered


async def run_signal_monitor() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    session_factory = create_session_factory(settings.database_url)
    previous: dict[str, str] = {}
    last_recovery_at: datetime | None = None
    while True:
        try:
            async with session_factory() as session:
                now = datetime.now(UTC)
                if last_recovery_at is None or (now - last_recovery_at).total_seconds() >= 5:
                    await reconcile_convert_executions(session)
                    await session.commit()
                    last_recovery_at = now
                candidates = await scan_configured_symbols(
                    session, settings.ingest_symbol_list, settings.ingest_interval
                )
                if settings.live_convert_enabled:
                    try:
                        async with BinanceRestClient(
                            str(settings.live_convert_rest_base_url),
                            settings.binance_timeout_seconds,
                        ) as public_client:
                            live_price = await public_client.get_ticker_price("XRPUSDT")
                        await evaluate_convert_limits(
                            session, symbol="XRPUSDT", price=Decimal(live_price)
                        )
                    except (BinanceRestError, ValueError, ArithmeticError):
                        logger.warning(
                            "production market price unavailable; live Convert plans "
                            "were not evaluated",
                            extra={"event_type": "live_market_data_unavailable"},
                        )
                else:
                    latest = await session.execute(
                        select(Candle)
                        .where(
                            Candle.symbol == "XRPUSDT",
                            Candle.interval == settings.ingest_interval,
                            Candle.environment == "sandbox",
                            Candle.is_closed.is_(True),
                        )
                        .order_by(desc(Candle.open_time))
                        .limit(1)
                    )
                    latest_candle = latest.scalar_one_or_none()
                    if latest_candle is not None:
                        age_seconds = (
                            datetime.now(UTC) - latest_candle.received_at
                        ).total_seconds()
                        if age_seconds <= settings.market_data_max_age_seconds:
                            await evaluate_convert_limits(
                                session,
                                symbol="XRPUSDT",
                                price=latest_candle.close_price,
                            )
                for candidate in candidates:
                    state_key = f"{candidate.status}:{candidate.side}:{candidate.confidence}"
                    if (
                        candidate.status == "candidate"
                        and previous.get(candidate.symbol) != state_key
                    ):
                        await record_audit_event(
                            session,
                            "signal_candidate_detected",
                            "signal_monitor",
                            candidate_payload(candidate),
                            candidate.symbol,
                        )
                        logger.info(
                            "signal candidate detected",
                            extra={
                                "event_type": "signal_candidate_detected",
                                "symbol": candidate.symbol,
                            },
                        )
                    previous[candidate.symbol] = state_key
                await session.commit()
        except Exception:
            logger.exception("signal monitor iteration failed")
        await asyncio.sleep(settings.signal_scan_seconds)


def main() -> None:
    try:
        asyncio.run(run_signal_monitor())
    except KeyboardInterrupt:
        logger.info("signal monitor stopped", extra={"event_type": "signal_monitor_stop"})
    finally:
        asyncio.run(dispose_engines())


if __name__ == "__main__":
    main()
