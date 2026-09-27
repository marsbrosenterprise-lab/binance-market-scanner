from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from binance_scanner.config import get_settings
from binance_scanner.database import create_session_factory
from binance_scanner.binance.convert import BinanceConvertClient, BinanceConvertError
from binance_scanner.indicators import CandlePoint
from binance_scanner.logging import configure_logging
from binance_scanner.models import Candle, ConvertRequest
from binance_scanner.proposals import record_audit_event
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
            .where(Candle.symbol == symbol, Candle.interval == interval)
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


async def evaluate_convert_limits(
    session: AsyncSession, *, symbol: str, price: Decimal
) -> int:
    """Trigger matching plans and execute only explicitly armed plans.

    Unarmed plans only transition to ``triggered``. Armed plans request a fresh
    Binance quote, verify that its effective rate still satisfies the trigger,
    and accept it once. This is deliberately one-shot and never re-arms a plan.
    """
    now = datetime.now(UTC)
    result = await session.execute(select(ConvertRequest).where(
        ConvertRequest.state == "watching", ConvertRequest.limit_price.is_not(None)
    ))
    triggered = 0
    for plan in result.scalars().all():
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
            matches = symbol == "XRPUSDT" and price <= plan.limit_price
        elif plan.from_asset == "XRP" and plan.to_asset == "USDT":
            matches = symbol == "XRPUSDT" and price >= plan.limit_price
        else:
            matches = False
        if not matches:
            continue
        triggered += 1
        trigger_details = {
            "request_id": plan.id,
            "symbol": symbol,
            "market_price": str(price),
            "limit_price": str(plan.limit_price),
            "trigger_direction": plan.trigger_direction,
        }
        if not plan.auto_execute or not get_settings().live_convert_auto_execution_enabled:
            plan.state = "triggered"
            plan.version += 1
            plan.triggered_at = now
            await record_audit_event(
                session, "convert_limit_triggered", "convert_monitor", trigger_details, str(plan.id)
            )
            continue

        # Auto execution is opt-in per plan. The service performs a fresh
        # quote check immediately before accepting; the stored trigger price
        # is never treated as the Binance execution price.
        settings = get_settings()
        key = settings.live_convert_api_key
        secret = settings.live_convert_api_secret
        try:
            if key is None or secret is None:
                raise RuntimeError("live Convert credentials are not configured")
            async with BinanceConvertClient(
                str(settings.live_convert_rest_base_url),
                key.get_secret_value(),
                secret.get_secret_value(),
                enabled=True,
                timeout_seconds=settings.binance_timeout_seconds,
            ) as client:
                quote = await client.get_quote(plan.from_asset, plan.to_asset, str(plan.from_amount))
                quote_id = quote.get("quoteId")
                from_amount = Decimal(str(quote.get("fromAmount", plan.from_amount)))
                to_amount = Decimal(str(quote.get("toAmount", "0")))
                if not isinstance(quote_id, str) or not quote_id or to_amount <= 0:
                    raise RuntimeError("fresh Binance quote was incomplete")
                effective_rate = (
                    from_amount / to_amount
                    if plan.from_asset == "USDT"
                    else to_amount / from_amount
                )
                acceptable = (
                    effective_rate <= plan.limit_price
                    if plan.from_asset == "USDT"
                    else effective_rate >= plan.limit_price
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
                accepted = await client.accept_quote(quote_id)
                order_id = accepted.get("orderId")
                status_payload = await client.order_status(
                    order_id=str(order_id) if order_id is not None else None,
                    quote_id=quote_id,
                )
            plan.quote_id = quote_id
            plan.quote_json = json.dumps(dict(quote), separators=(",", ":"))
            plan.order_id = str(order_id) if order_id is not None else None
            plan.order_status = str(status_payload.get("orderStatus", "UNKNOWN"))
            plan.state = "completed"
            plan.version += 1
            plan.triggered_at = now
            plan.completed_at = now
            await record_audit_event(
                session,
                "convert_auto_execution_completed",
                "convert_monitor",
                {**trigger_details, "order_id": plan.order_id, "order_status": plan.order_status},
                str(plan.id),
            )
        except (BinanceConvertError, RuntimeError, ValueError) as exc:
            # Keep the plan triggered for manual review/retry; never loop an
            # automatic acceptance after an exchange or quote failure.
            plan.state = "triggered"
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


async def run_signal_monitor() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    session_factory = create_session_factory(settings.database_url)
    previous: dict[str, str] = {}
    while True:
        try:
            async with session_factory() as session:
                candidates = await scan_configured_symbols(
                    session, settings.ingest_symbol_list, settings.ingest_interval
                )
                latest = await session.execute(
                    select(Candle)
                    .where(Candle.symbol == "XRPUSDT", Candle.interval == settings.ingest_interval)
                    .order_by(desc(Candle.open_time))
                    .limit(1)
                )
                latest_candle = latest.scalar_one_or_none()
                if latest_candle is not None:
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


if __name__ == "__main__":
    main()
