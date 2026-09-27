from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from binance_scanner.auth import AuthenticatedOperator, authenticate_operator
from binance_scanner.backtest import BacktestCandle, BacktestConfig, BacktestEngine
from binance_scanner.backtest_io import HistoricalCandle, render_candles_csv
from binance_scanner.binance.account import (
    BinanceAccountClient,
    BinanceAccountError,
    non_zero_balances,
)
from binance_scanner.binance.convert import BinanceConvertClient, BinanceConvertError
from binance_scanner.binance.filters import normalize_risk_quantity, validate_order_intent
from binance_scanner.binance.rest import BinanceRestClient, BinanceRestError
from binance_scanner.binance.trading import BinanceTradingClient
from binance_scanner.config import get_settings
from binance_scanner.database import check_database, create_session_factory, dispose_engines
from binance_scanner.execution import (
    DryRunOrderExecutor,
    OrderExecutionError,
    OrderExecutionUncertain,
    order_intent_from_proposal,
)
from binance_scanner.indicators import CandlePoint, IndicatorEngine
from binance_scanner.logging import configure_logging
from binance_scanner.market_data import parse_exchange_symbols
from binance_scanner.models import (
    AuditEvent,
    Candle,
    ConvertRequest,
    Symbol,
    TradeExecution,
    TradeProposal,
)
from binance_scanner.proposals import (
    approve_proposal,
    create_proposal,
    expire_pending_proposals,
    record_audit_event,
)
from binance_scanner.risk import RiskEngine
from binance_scanner.signal_monitor import candidate_payload, scan_configured_symbols
from binance_scanner.strategies import TrendMomentumStrategy

# ruff: noqa: E501  # Embedded dashboard HTML/JavaScript intentionally uses long lines.


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        yield
    finally:
        await dispose_engines()


app = FastAPI(
    title="Binance Market Scanner",
    version="0.1.0",
    description="Read-only Phase 1 foundation for the Binance Spot Testnet scanner.",
    lifespan=lifespan,
)


class ProposalRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    interval: str = Field(default="1m", min_length=1, max_length=16)
    account_balance: float = Field(gt=0)
    current_exposure: float = Field(default=0, ge=0)
    available_balance: float | None = Field(default=None, ge=0)
    candle_limit: int = Field(default=200, ge=20, le=1000)


class ApprovalRequest(BaseModel):
    expected_version: int = Field(ge=1)


class ExecutionRequest(BaseModel):
    expected_version: int = Field(ge=1)


class ConvertQuoteRequest(BaseModel):
    from_asset: str = Field(min_length=2, max_length=16)
    to_asset: str = Field(min_length=2, max_length=16)
    from_amount: Decimal = Field(ge=Decimal("0.01"))


class ConvertLimitRequest(BaseModel):
    from_asset: str = Field(min_length=2, max_length=16)
    to_asset: str = Field(min_length=2, max_length=16)
    from_amount: Decimal = Field(ge=Decimal("0.01"))
    limit_price: Decimal = Field(gt=0)
    expires_in_days: int = Field(default=30, ge=1, le=30)


class ConvertApprovalRequest(BaseModel):
    expected_version: int = Field(ge=1)


class BacktestRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    interval: str = Field(default="1m", min_length=1, max_length=16)
    initial_balance: float = Field(default=10_000, gt=0)
    candle_limit: int = Field(default=1000, ge=20, le=5000)
    warmup_candles: int = Field(default=30, ge=1, le=1000)
    fee_fraction: float = Field(default=0.001, ge=0, lt=1)
    slippage_fraction: float = Field(default=0.0005, ge=0, lt=1)


def require_approval_token(
    x_approval_token: str | None = Header(default=None),
) -> AuthenticatedOperator:
    settings = get_settings()
    configured = settings.approval_token
    try:
        return authenticate_operator(
            x_approval_token,
            configured.get_secret_value() if configured else None,
            settings.operator_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


def require_live_convert_mode(
    x_account_mode: str | None = Header(default=None),
) -> None:
    if x_account_mode != "live":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="select Live Convert mode before using live funds",
        )


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def readiness() -> dict[str, str]:
    settings = get_settings()
    if not await check_database(settings.database_url):
        raise HTTPException(status_code=503, detail="database unavailable")
    return {
        "status": "ok",
        "mode": settings.app_mode,
        "trading_enabled": str(settings.trading_enabled).lower(),
    }


@app.get("/api/v1/market/symbols", tags=["market-data"])
async def market_symbols() -> list[dict[str, object]]:
    settings = get_settings()
    async with BinanceRestClient(
        str(settings.binance_rest_base_url), settings.binance_timeout_seconds
    ) as client:
        payload = await client.get_exchange_info()
    return [
        {
            "symbol": item.symbol,
            "base_asset": item.base_asset,
            "quote_asset": item.quote_asset,
            "status": item.status,
            "is_spot_trading_allowed": item.is_spot_trading_allowed,
        }
        for item in parse_exchange_symbols(payload, settings.market_data_quote_asset)
    ]


@app.get("/api/v1/account/balances", tags=["account"])
async def account_balances(
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
) -> dict[str, object]:
    settings = get_settings()
    api_key = settings.binance_api_key
    api_secret = settings.binance_api_secret
    if api_key is None or api_secret is None:
        raise HTTPException(status_code=503, detail="Binance API credentials are not configured")
    try:
        async with BinanceAccountClient(
            str(settings.binance_rest_base_url),
            api_key.get_secret_value(),
            api_secret.get_secret_value(),
            timeout_seconds=settings.binance_timeout_seconds,
        ) as client:
            account = await client.get_account()
    except (BinanceAccountError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Binance account verification failed") from exc
    balances = non_zero_balances(account)
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        await record_audit_event(
            session,
            "account_balance_checked",
            "account_service",
            {"operator_id": operator.operator_id, "non_zero_asset_count": len(balances)},
        )
        await session.commit()
    return {
        "account_type": "SPOT_DEMO"
        if settings.app_mode in {"read_only_demo", "approval_demo"}
        else "SPOT_TESTNET",
        "can_trade": bool(account.get("canTrade", False)),
        "balances": balances,
    }


@app.get("/api/v1/account/live-balances", tags=["account"])
async def live_account_balances(
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    _: Annotated[None, Depends(require_live_convert_mode)],
) -> dict[str, object]:
    settings = get_settings()
    api_key = settings.live_convert_api_key
    api_secret = settings.live_convert_api_secret
    if api_key is None or api_secret is None:
        raise HTTPException(status_code=503, detail="Live Convert credentials are not configured")
    try:
        async with BinanceAccountClient(
            str(settings.live_convert_rest_base_url),
            api_key.get_secret_value(),
            api_secret.get_secret_value(),
            allow_live=True,
            timeout_seconds=settings.binance_timeout_seconds,
        ) as client:
            account = await client.get_account()
    except (BinanceAccountError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail="Live Binance account verification failed"
        ) from exc
    balances = non_zero_balances(account)
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        await record_audit_event(
            session,
            "live_account_balance_checked",
            "live_account_service",
            {"operator_id": operator.operator_id, "non_zero_asset_count": len(balances)},
        )
        await session.commit()
    return {
        "account_type": "SPOT_LIVE",
        "can_trade": bool(account.get("canTrade", False)),
        "balances": balances,
    }


def _convert_response(request: ConvertRequest) -> dict[str, object]:
    quote = json.loads(request.quote_json)
    return {
        "request_id": request.id,
        "state": request.state,
        "version": request.version,
        "from_asset": request.from_asset,
        "to_asset": request.to_asset,
        "from_amount": str(request.from_amount),
        "limit_price": str(request.limit_price) if request.limit_price is not None else None,
        "trigger_direction": request.trigger_direction,
        "auto_execute": request.auto_execute,
        "armed_by": request.armed_by,
        "armed_at": request.armed_at,
        "expires_at": request.expires_at,
        "triggered_at": request.triggered_at,
        "quote": quote,
        "quote_id": request.quote_id,
        "order_id": request.order_id,
        "order_status": request.order_status,
        "approved_at": request.approved_at,
        "approval_actor": request.approval_actor,
        "created_at": request.created_at,
        "completed_at": request.completed_at,
    }


def _convert_client(settings: object) -> BinanceConvertClient:
    from binance_scanner.config import Settings

    if not isinstance(settings, Settings):
        raise RuntimeError("invalid application settings")
    if not settings.live_convert_enabled:
        raise RuntimeError("live Convert is disabled")
    key = settings.live_convert_api_key
    secret = settings.live_convert_api_secret
    if key is None or secret is None:
        raise RuntimeError("live Convert credentials are not configured")
    return BinanceConvertClient(
        str(settings.live_convert_rest_base_url),
        key.get_secret_value(),
        secret.get_secret_value(),
        enabled=True,
        timeout_seconds=settings.binance_timeout_seconds,
    )


@app.post("/api/v1/convert/quotes", tags=["live-convert"])
async def request_convert_quote(
    request: ConvertQuoteRequest,
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    _: Annotated[None, Depends(require_live_convert_mode)],
) -> dict[str, object]:
    settings = get_settings()
    from_asset = request.from_asset.upper()
    to_asset = request.to_asset.upper()
    if from_asset not in settings.live_convert_from_asset_list:
        raise HTTPException(status_code=422, detail="from asset is not allowed for live Convert")
    if to_asset not in settings.live_convert_to_asset_list or to_asset == from_asset:
        raise HTTPException(status_code=422, detail="to asset is not allowed for live Convert")
    if request.from_amount < Decimal(str(settings.live_convert_min_from_amount)):
        raise HTTPException(status_code=422, detail="conversion amount must be at least 0.01 USDT")
    if request.from_amount > Decimal(str(settings.live_convert_max_from_amount)):
        raise HTTPException(status_code=422, detail="conversion exceeds configured amount cap")
    try:
        async with _convert_client(settings) as client:
            quote = await client.get_quote(from_asset, to_asset, str(request.from_amount))
    except (BinanceConvertError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    quote_id = quote.get("quoteId")
    if not isinstance(quote_id, str) or not quote_id:
        raise HTTPException(
            status_code=422,
            detail=(
                "Binance returned no quoteId. The Live Convert account may not have enough "
                "available balance for this asset pair. The dashboard account above is Demo; "
                "verify the real Binance Spot wallet has sufficient USDT/XRP."
            ),
        )
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        convert_request = ConvertRequest(
            from_asset=from_asset,
            to_asset=to_asset,
            from_amount=request.from_amount,
            state="quoted",
            version=1,
            quote_id=quote_id,
            quote_json=json.dumps(dict(quote), separators=(",", ":")),
        )
        session.add(convert_request)
        await session.flush()
        await record_audit_event(
            session,
            "convert_quote_requested",
            "convert_service",
            {
                "request_id": convert_request.id,
                "operator_id": operator.operator_id,
                "from_asset": from_asset,
                "to_asset": to_asset,
                "from_amount": str(request.from_amount),
                "quote_id": quote_id,
            },
            str(convert_request.id),
        )
        await session.commit()
        await session.refresh(convert_request)
        return _convert_response(convert_request)


@app.post("/api/v1/convert/preview", tags=["live-convert"])
async def preview_convert_rate(
    request: ConvertQuoteRequest,
    _: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    __: Annotated[None, Depends(require_live_convert_mode)],
) -> dict[str, object]:
    """Fetch a live Convert rate without creating or executing a request."""
    settings = get_settings()
    from_asset = request.from_asset.upper()
    to_asset = request.to_asset.upper()
    if from_asset not in settings.live_convert_from_asset_list:
        raise HTTPException(status_code=422, detail="from asset is not allowed for live Convert")
    if to_asset not in settings.live_convert_to_asset_list or to_asset == from_asset:
        raise HTTPException(status_code=422, detail="to asset is not allowed for live Convert")
    if request.from_amount < Decimal(str(settings.live_convert_min_from_amount)):
        raise HTTPException(status_code=422, detail="conversion amount must be at least 0.01 USDT")
    if request.from_amount > Decimal(str(settings.live_convert_max_from_amount)):
        raise HTTPException(status_code=422, detail="conversion exceeds configured amount cap")
    try:
        async with _convert_client(settings) as client:
            quote = await client.get_quote(from_asset, to_asset, str(request.from_amount))
    except (BinanceConvertError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        quoted_from = Decimal(str(quote["fromAmount"]))
        quoted_to = Decimal(str(quote["toAmount"]))
        if quoted_from <= 0 or quoted_to <= 0:
            raise ValueError
        effective_rate = (
            quoted_from / quoted_to if from_asset == "USDT" else quoted_to / quoted_from
        )
    except (KeyError, ValueError, ArithmeticError) as exc:
        raise HTTPException(
            status_code=502, detail="Binance returned an incomplete Convert quote"
        ) from exc
    return {
        "from_asset": from_asset,
        "to_asset": to_asset,
        "from_amount": str(quoted_from),
        "to_amount": str(quoted_to),
        "effective_rate": str(effective_rate),
        "quote_id": quote.get("quoteId"),
        "valid_timestamp": quote.get("validTimestamp"),
    }


@app.post("/api/v1/convert/limits", tags=["live-convert"])
async def create_convert_limit(
    request: ConvertLimitRequest,
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    _: Annotated[None, Depends(require_live_convert_mode)],
) -> dict[str, object]:
    """Create an application-managed Limit Convert plan.

    Binance's public Convert API exposes quote/accept, while the web UI also
    offers Limit Convert. This plan safely bridges that gap: the monitor marks
    it triggered at the requested price, then a fresh quote must be reviewed,
    approved, and accepted explicitly.
    """
    settings = get_settings()
    from_asset = request.from_asset.upper()
    to_asset = request.to_asset.upper()
    if from_asset not in settings.live_convert_from_asset_list:
        raise HTTPException(status_code=422, detail="from asset is not allowed for live Convert")
    if to_asset not in settings.live_convert_to_asset_list or to_asset == from_asset:
        raise HTTPException(status_code=422, detail="to asset is not allowed for live Convert")
    if request.from_amount < Decimal(str(settings.live_convert_min_from_amount)):
        raise HTTPException(status_code=422, detail="limit plan amount must be at least 0.01 USDT")
    if request.from_amount > Decimal(str(settings.live_convert_max_from_amount)):
        raise HTTPException(status_code=422, detail="conversion exceeds configured amount cap")
    if {from_asset, to_asset} != {"USDT", "XRP"}:
        raise HTTPException(
            status_code=422, detail="Limit Convert currently supports USDT/XRP only"
        )
    trigger_direction = "at_or_below" if to_asset == "XRP" else "at_or_above"
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        plan = ConvertRequest(
            from_asset=from_asset,
            to_asset=to_asset,
            from_amount=request.from_amount,
            limit_price=request.limit_price,
            trigger_direction=trigger_direction,
            expires_at=datetime.now(UTC) + timedelta(days=request.expires_in_days),
            state="watching",
            version=1,
            quote_json="{}",
        )
        session.add(plan)
        await session.flush()
        await record_audit_event(
            session,
            "convert_limit_created",
            "convert_service",
            {
                "request_id": plan.id,
                "operator_id": operator.operator_id,
                "from_asset": from_asset,
                "to_asset": to_asset,
                "from_amount": str(request.from_amount),
                "limit_price": str(request.limit_price),
                "trigger_direction": trigger_direction,
            },
            str(plan.id),
        )
        await session.commit()
        await session.refresh(plan)
        return _convert_response(plan)


@app.post("/api/v1/convert/limits/{request_id}/quote", tags=["live-convert"])
async def quote_triggered_convert_limit(
    request_id: int,
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    _: Annotated[None, Depends(require_live_convert_mode)],
) -> dict[str, object]:
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(ConvertRequest).where(ConvertRequest.id == request_id).with_for_update()
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            raise HTTPException(status_code=404, detail="Convert limit plan not found")
        if plan.state != "triggered":
            raise HTTPException(status_code=409, detail="Limit plan is not triggered")
        if plan.expires_at and plan.expires_at <= datetime.now(UTC):
            plan.state = "expired"
            plan.version += 1
            await session.commit()
            raise HTTPException(status_code=409, detail="Limit plan has expired")
        try:
            async with _convert_client(settings) as client:
                quote = await client.get_quote(
                    plan.from_asset, plan.to_asset, str(plan.from_amount)
                )
        except (BinanceConvertError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        quote_id = quote.get("quoteId")
        if not isinstance(quote_id, str) or not quote_id:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Binance returned no quoteId. The Live Convert account may not have enough "
                    "available balance for this asset pair. Verify the real Binance Spot wallet."
                ),
            )
        plan.quote_id = quote_id
        plan.quote_json = json.dumps(dict(quote), separators=(",", ":"))
        plan.state = "quoted"
        plan.version += 1
        await record_audit_event(
            session,
            "convert_limit_triggered_quote_requested",
            "convert_service",
            {"request_id": request_id, "operator_id": operator.operator_id, "quote_id": quote_id},
            str(request_id),
        )
        await session.commit()
        await session.refresh(plan)
        return _convert_response(plan)


@app.post("/api/v1/convert/limits/{request_id}/cancel", tags=["live-convert"])
async def cancel_convert_limit(
    request_id: int,
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    _: Annotated[None, Depends(require_live_convert_mode)],
) -> dict[str, object]:
    session_factory = create_session_factory(get_settings().database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(ConvertRequest).where(ConvertRequest.id == request_id).with_for_update()
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            raise HTTPException(status_code=404, detail="Convert limit plan not found")
        if plan.state not in {"watching", "triggered"}:
            raise HTTPException(status_code=409, detail="Limit plan cannot be cancelled")
        plan.state = "cancelled"
        plan.version += 1
        await record_audit_event(
            session,
            "convert_limit_cancelled",
            "convert_service",
            {"request_id": request_id, "operator_id": operator.operator_id},
            str(request_id),
        )
        await session.commit()
        await session.refresh(plan)
        return _convert_response(plan)


@app.post("/api/v1/convert/limits/{request_id}/arm", tags=["live-convert"])
async def arm_convert_limit(
    request_id: int,
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    _: Annotated[None, Depends(require_live_convert_mode)],
) -> dict[str, object]:
    """Explicitly authorize one unattended execution when the trigger is reached."""
    settings = get_settings()
    if not settings.live_convert_enabled or not settings.live_convert_auto_execution_enabled:
        raise HTTPException(
            status_code=409,
            detail=(
                "Live Convert and automatic execution must both be enabled before arming a plan."
            ),
        )
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(ConvertRequest).where(ConvertRequest.id == request_id).with_for_update()
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            raise HTTPException(status_code=404, detail="Convert limit plan not found")
        if plan.limit_price is None or plan.state != "watching":
            raise HTTPException(status_code=409, detail="Only a watching limit plan can be armed")
        if plan.expires_at and plan.expires_at <= datetime.now(UTC):
            plan.state = "expired"
            plan.version += 1
            await session.commit()
            raise HTTPException(status_code=409, detail="Limit plan has expired")
        plan.auto_execute = True
        plan.armed_by = operator.operator_id
        plan.armed_at = datetime.now(UTC)
        plan.version += 1
        await record_audit_event(
            session,
            "convert_limit_armed",
            "convert_service",
            {"request_id": request_id, "operator_id": operator.operator_id},
            str(request_id),
        )
        await session.commit()
        await session.refresh(plan)
        return _convert_response(plan)


@app.post("/api/v1/convert/limits/{request_id}/disarm", tags=["live-convert"])
async def disarm_convert_limit(
    request_id: int,
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    _: Annotated[None, Depends(require_live_convert_mode)],
) -> dict[str, object]:
    session_factory = create_session_factory(get_settings().database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(ConvertRequest).where(ConvertRequest.id == request_id).with_for_update()
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            raise HTTPException(status_code=404, detail="Convert limit plan not found")
        if plan.state != "watching":
            raise HTTPException(
                status_code=409, detail="Only a watching limit plan can be disarmed"
            )
        plan.auto_execute = False
        plan.armed_by = None
        plan.armed_at = None
        plan.version += 1
        await record_audit_event(
            session,
            "convert_limit_disarmed",
            "convert_service",
            {"request_id": request_id, "operator_id": operator.operator_id},
            str(request_id),
        )
        await session.commit()
        await session.refresh(plan)
        return _convert_response(plan)


@app.get("/api/v1/convert/requests", tags=["live-convert"])
async def list_convert_requests(
    _: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    live_mode: Annotated[None, Depends(require_live_convert_mode)],
) -> list[dict[str, object]]:
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(ConvertRequest).order_by(ConvertRequest.created_at.desc()).limit(50)
        )
        return [_convert_response(item) for item in result.scalars().all()]


@app.post("/api/v1/convert/requests/{request_id}/approve", tags=["live-convert"])
async def approve_convert_request(
    request_id: int,
    request: ConvertApprovalRequest,
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    _: Annotated[None, Depends(require_live_convert_mode)],
) -> dict[str, object]:
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(ConvertRequest).where(ConvertRequest.id == request_id).with_for_update()
        )
        convert_request = result.scalar_one_or_none()
        if convert_request is None:
            raise HTTPException(status_code=404, detail="Convert request not found")
        if convert_request.state != "quoted" or convert_request.version != request.expected_version:
            raise HTTPException(status_code=409, detail="Convert request is stale or not quotable")
        quote = json.loads(convert_request.quote_json)
        valid_timestamp = quote.get("validTimestamp")
        if valid_timestamp is not None and int(valid_timestamp) <= int(
            datetime.now(UTC).timestamp() * 1000
        ):
            convert_request.state = "expired"
            convert_request.version += 1
            await session.commit()
            raise HTTPException(status_code=409, detail="Convert quote has expired")
        convert_request.state = "approved"
        convert_request.version += 1
        convert_request.approval_actor = operator.operator_id
        convert_request.approved_at = datetime.now(UTC)
        await record_audit_event(
            session,
            "convert_approved",
            "convert_service",
            {"request_id": request_id, "operator_id": operator.operator_id},
            str(request_id),
        )
        await session.commit()
        await session.refresh(convert_request)
        return _convert_response(convert_request)


@app.post("/api/v1/convert/requests/{request_id}/execute", tags=["live-convert"])
async def execute_convert_request(
    request_id: int,
    request: ConvertApprovalRequest,
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    _: Annotated[None, Depends(require_live_convert_mode)],
) -> dict[str, object]:
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(ConvertRequest).where(ConvertRequest.id == request_id).with_for_update()
        )
        convert_request = result.scalar_one_or_none()
        if convert_request is None:
            raise HTTPException(status_code=404, detail="Convert request not found")
        if (
            convert_request.state != "approved"
            or convert_request.version != request.expected_version
        ):
            raise HTTPException(status_code=409, detail="Convert request is not approved or stale")
        quote_id = convert_request.quote_id
        if not quote_id:
            raise HTTPException(status_code=422, detail="Convert request has no quote ID")
        accepted_started = False
        try:
            async with _convert_client(settings) as client:
                accepted = await client.accept_quote(quote_id)
                accepted_started = True
                order_id = accepted.get("orderId")
                status_payload = await client.order_status(
                    order_id=str(order_id) if order_id is not None else None, quote_id=quote_id
                )
        except (BinanceConvertError, RuntimeError, ValueError) as exc:
            convert_request.state = "reconciliation_required" if accepted_started else "failed"
            convert_request.reconciliation_required = accepted_started
            convert_request.version += 1
            await record_audit_event(
                session,
                "convert_failed",
                "convert_service",
                {"request_id": request_id, "operator_id": operator.operator_id},
                str(request_id),
            )
            await session.commit()
            raise HTTPException(status_code=502, detail="live Convert execution failed") from exc
        convert_request.order_status = str(status_payload.get("orderStatus", "UNKNOWN"))
        if convert_request.order_status == "SUCCESS":
            convert_request.state = "completed"
            convert_request.reconciliation_required = False
        elif convert_request.order_status in {"PROCESS", "PENDING"}:
            convert_request.state = "processing"
            convert_request.reconciliation_required = True
        elif convert_request.order_status in {"FAIL", "FAILED", "CANCELED", "EXPIRED"}:
            convert_request.state = "failed"
            convert_request.reconciliation_required = False
        else:
            convert_request.state = "reconciliation_required"
            convert_request.reconciliation_required = True
        convert_request.version += 1
        convert_request.order_id = str(order_id) if order_id is not None else None
        convert_request.completed_at = (
            datetime.now(UTC) if convert_request.state == "completed" else None
        )
        await record_audit_event(
            session,
            "convert_completed",
            "convert_service",
            {
                "request_id": request_id,
                "operator_id": operator.operator_id,
                "order_id": convert_request.order_id,
                "order_status": convert_request.order_status,
            },
            str(request_id),
        )
        await session.commit()
        await session.refresh(convert_request)
        return _convert_response(convert_request)


@app.get("/api/v1/market/indicators/{symbol}", tags=["market-data"])
async def market_indicators(
    symbol: str,
    interval: str = Query(default="1m", min_length=1, max_length=16),
    limit: int = Query(default=200, ge=20, le=1000),
) -> dict[str, object]:
    normalized_symbol = symbol.upper()
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(Candle)
            .where(Candle.symbol == normalized_symbol, Candle.interval == interval)
            .order_by(desc(Candle.open_time))
            .limit(limit)
        )
        rows = list(reversed(result.scalars().all()))
    candles = [
        CandlePoint(
            high=float(row.high_price), low=float(row.low_price), close=float(row.close_price)
        )
        for row in rows
    ]
    snapshot = IndicatorEngine().compute(candles)
    return {
        "symbol": normalized_symbol,
        "interval": interval,
        "candle_count": len(candles),
        "indicators": snapshot.values,
    }


@app.get("/api/v1/market/price/{symbol}", tags=["market-data"])
async def market_price(
    symbol: str,
    interval: str = Query(default="1m", min_length=1, max_length=16),
    live: bool = Query(default=False),
) -> dict[str, object]:
    """Return a sandbox candle price or a production public ticker explicitly."""
    normalized_symbol = symbol.upper()
    settings = get_settings()
    if live:
        try:
            async with BinanceRestClient(
                str(settings.live_convert_rest_base_url), settings.binance_timeout_seconds
            ) as client:
                price = await client.get_ticker_price(normalized_symbol)
        except (BinanceRestError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="live market price unavailable") from exc
        return {
            "symbol": normalized_symbol,
            "interval": interval,
            "price": price,
            "updated_at": datetime.now(UTC).isoformat(),
            "source": "Binance production public ticker",
        }
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(Candle)
            .where(Candle.symbol == normalized_symbol, Candle.interval == interval)
            .order_by(desc(Candle.open_time))
            .limit(1)
        )
        candle = result.scalar_one_or_none()
    if candle is None:
        raise HTTPException(status_code=404, detail="No market price is available yet")
    age_seconds = (datetime.now(UTC) - candle.received_at).total_seconds()
    if age_seconds > settings.market_data_max_age_seconds:
        raise HTTPException(status_code=503, detail="sandbox market data is stale")
    return {
        "symbol": normalized_symbol,
        "interval": interval,
        "price": str(candle.close_price),
        "updated_at": candle.received_at.isoformat(),
        "source": "Binance sandbox WebSocket kline stream",
    }


@app.get("/api/v1/signals", tags=["signals"])
async def market_signals() -> list[dict[str, object]]:
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        candidates = await scan_configured_symbols(
            session, settings.ingest_symbol_list, settings.ingest_interval
        )
    return [candidate_payload(candidate) for candidate in candidates]


@app.get("/api/v1/signals/{symbol}", tags=["signals"])
async def market_signal(
    symbol: str,
    interval: str = Query(default="1m", min_length=1, max_length=16),
    limit: int = Query(default=200, ge=20, le=1000),
) -> dict[str, object]:
    normalized_symbol = symbol.upper()
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(Candle)
            .where(Candle.symbol == normalized_symbol, Candle.interval == interval)
            .order_by(desc(Candle.open_time))
            .limit(limit)
        )
        rows = list(reversed(result.scalars().all()))
    candles = [
        CandlePoint(
            high=float(row.high_price), low=float(row.low_price), close=float(row.close_price)
        )
        for row in rows
    ]
    candidate = TrendMomentumStrategy().evaluate(normalized_symbol, interval, candles)
    return {
        "symbol": candidate.symbol,
        "interval": candidate.interval,
        "strategy": candidate.strategy_name,
        "strategy_version": candidate.strategy_version,
        "status": candidate.status,
        "side": candidate.side,
        "confidence": candidate.confidence,
        "entry_price": candidate.entry_price,
        "stop_price": candidate.stop_price,
        "target_price": candidate.target_price,
        "risk_reward": candidate.risk_reward,
        "rationale": candidate.rationale,
        "candle_count": len(candles),
    }


@app.post("/api/v1/proposals", tags=["approvals"])
async def create_trade_proposal(
    request: ProposalRequest,
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
) -> dict[str, object]:
    normalized_symbol = request.symbol.upper()
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        account_balance = request.account_balance
        risk_source = "request_simulation"
        api_key = settings.binance_api_key
        api_secret = settings.binance_api_secret
        if api_key is not None and api_secret is not None:
            try:
                async with BinanceAccountClient(
                    str(settings.binance_rest_base_url),
                    api_key.get_secret_value(),
                    api_secret.get_secret_value(),
                    timeout_seconds=settings.binance_timeout_seconds,
                ) as account_client:
                    account = await account_client.get_account()
            except (BinanceAccountError, ValueError) as exc:
                raise HTTPException(
                    status_code=502, detail="server-side balance verification failed"
                ) from exc
            balances = {
                str(item.get("asset")): float(item.get("free", 0))
                for item in account.get("balances", [])
                if isinstance(item, dict)
            }
            account_balance = balances.get(settings.market_data_quote_asset, 0.0)
            risk_source = "binance_account"
        elif settings.app_mode not in {"paper", "backtest"}:
            raise HTTPException(
                status_code=503,
                detail="server-side Binance credentials are required for risk checks",
            )

        exposure_result = await session.execute(
            select(TradeExecution, TradeProposal)
            .join(TradeProposal, TradeProposal.id == TradeExecution.proposal_id)
            .where(
                TradeExecution.status.in_(
                    ["pending", "new", "partially_filled", "reconciliation_required"]
                )
            )
        )
        current_exposure = 0.0
        for _, open_proposal in exposure_result.all():
            try:
                current_exposure += float(json.loads(open_proposal.risk_json).get("notional", 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        available_balance = max(account_balance - current_exposure, 0.0)
        result = await session.execute(
            select(Candle)
            .where(Candle.symbol == normalized_symbol, Candle.interval == request.interval)
            .order_by(desc(Candle.open_time))
            .limit(request.candle_limit)
        )
        rows = list(reversed(result.scalars().all()))
        candles = [
            CandlePoint(
                high=float(row.high_price), low=float(row.low_price), close=float(row.close_price)
            )
            for row in rows
        ]
        candidate = TrendMomentumStrategy().evaluate(normalized_symbol, request.interval, candles)
        risk = RiskEngine().assess(
            candidate,
            account_balance,
            current_exposure,
            available_balance,
        )
        symbol_result = await session.execute(
            select(Symbol).where(Symbol.symbol == normalized_symbol)
        )
        symbol = symbol_result.scalar_one_or_none()
        if symbol is not None:
            risk = normalize_risk_quantity(
                risk,
                candidate.entry_price,
                account_balance,
                symbol.filters_json,
                settings.max_order_notional,
            )
        proposal = await create_proposal(
            session, candidate, risk, settings.proposal_expiry_seconds, operator.operator_id
        )
        await record_audit_event(
            session,
            "proposal_risk_snapshot",
            "risk_service",
            {
                "proposal_id": proposal.id,
                "account_balance": account_balance,
                "current_exposure": current_exposure,
                "available_balance": available_balance,
                "source": risk_source,
            },
            str(proposal.id),
        )
        await session.commit()
    return {
        "proposal_id": proposal.id,
        "state": proposal.state,
        "version": proposal.version,
        "symbol": proposal.symbol,
        "expires_at": proposal.expires_at,
        "signal": json.loads(proposal.signal_json),
        "risk": json.loads(proposal.risk_json),
    }


@app.get("/api/v1/proposals", tags=["approvals"])
async def pending_proposals(
    _: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
) -> list[dict[str, object]]:
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        await expire_pending_proposals(session)
        await session.commit()
        result = await session.execute(
            select(TradeProposal)
            .where(
                TradeProposal.state.in_(["pending", "approved"]),
                TradeProposal.expires_at > datetime.now(UTC),
            )
            .order_by(TradeProposal.created_at)
        )
        proposals = result.scalars().all()
    return [
        {
            "proposal_id": proposal.id,
            "symbol": proposal.symbol,
            "interval": proposal.interval,
            "state": proposal.state,
            "version": proposal.version,
            "expires_at": proposal.expires_at,
            "signal": json.loads(proposal.signal_json),
            "risk": json.loads(proposal.risk_json),
        }
        for proposal in proposals
    ]


@app.get("/api/v1/executions", tags=["execution"])
async def execution_history(
    _: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, object]]:
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(TradeExecution).order_by(desc(TradeExecution.created_at)).limit(limit)
        )
        executions = result.scalars().all()
    return [_execution_response(execution) for execution in executions]


@app.get("/api/v1/audit", tags=["audit"])
async def audit_events(
    _: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, object]]:
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(AuditEvent).order_by(desc(AuditEvent.created_at)).limit(limit)
        )
        events = result.scalars().all()
    return [
        {
            "id": event.id,
            "event_type": event.event_type,
            "component": event.component,
            "correlation_id": event.correlation_id,
            "payload": json.loads(event.payload_json),
            "created_at": event.created_at,
        }
        for event in events
    ]


@app.get("/api/v1/market/candles/{symbol}/csv", tags=["backtest"])
async def export_market_candles(
    symbol: str,
    interval: str = Query(default="1m", min_length=1, max_length=16),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> Response:
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(Candle)
            .where(Candle.symbol == symbol.upper(), Candle.interval == interval)
            .order_by(desc(Candle.open_time))
            .limit(limit)
        )
        candles = list(reversed(result.scalars().all()))
    rows = [
        HistoricalCandle(
            timestamp=candle.open_time,
            candle=BacktestCandle(
                open=float(candle.open_price),
                high=float(candle.high_price),
                low=float(candle.low_price),
                close=float(candle.close_price),
            ),
        )
        for candle in candles
    ]
    return Response(
        content=render_candles_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{symbol.upper()}-{interval}.csv"'},
    )


@app.post("/api/v1/backtests", tags=["backtest"])
async def run_backtest(request: BacktestRequest) -> dict[str, object]:
    normalized_symbol = request.symbol.upper()
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        result = await session.execute(
            select(Candle)
            .where(Candle.symbol == normalized_symbol, Candle.interval == request.interval)
            .order_by(desc(Candle.open_time))
            .limit(request.candle_limit)
        )
        stored_candles = list(reversed(result.scalars().all()))
    if len(stored_candles) <= request.warmup_candles:
        raise HTTPException(status_code=422, detail="not enough stored candles for backtest")
    try:
        backtest = BacktestEngine(
            config=BacktestConfig(
                initial_balance=request.initial_balance,
                warmup_candles=request.warmup_candles,
                fee_fraction=request.fee_fraction,
                slippage_fraction=request.slippage_fraction,
            )
        ).run(
            normalized_symbol,
            request.interval,
            [
                BacktestCandle(
                    open=float(candle.open_price),
                    high=float(candle.high_price),
                    low=float(candle.low_price),
                    close=float(candle.close_price),
                )
                for candle in stored_candles
            ],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "symbol": normalized_symbol,
        "interval": request.interval,
        "candle_count": len(stored_candles),
        "initial_balance": backtest.initial_balance,
        "ending_balance": backtest.ending_balance,
        "total_return_fraction": backtest.total_return_fraction,
        "win_rate": backtest.win_rate,
        "trades": [asdict(trade) for trade in backtest.trades],
    }


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> str:
    return """<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Binance Scanner Approvals</title>
<style>
:root{color-scheme:dark}body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:#0b1220;color:#e5e7eb}main{max-width:1200px;margin:0 auto;padding:2rem}h1{margin:.2rem 0}.muted{color:#9ca3af}.toolbar,.card{background:#111827;border:1px solid #263449;border-radius:12px;padding:1rem;margin:1rem 0}.toolbar{display:flex;gap:.7rem;align-items:center;flex-wrap:wrap}button{padding:.55rem .85rem;border:1px solid #4b5563;border-radius:7px;background:#1f2937;color:#fff;cursor:pointer}button:hover{background:#374151}button.primary{background:#2563eb;border-color:#3b82f6}button.warn{background:#92400e;border-color:#d97706}button:disabled{opacity:.45;cursor:not-allowed}input{padding:.55rem;background:#0f172a;color:#fff;border:1px solid #4b5563;border-radius:7px;min-width:280px}.status{min-height:1.5rem;padding:.5rem;border-radius:7px}.status.error{color:#fecaca;background:#450a0a}.status.ok{color:#bbf7d0;background:#052e16}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1rem}.signal h3{margin:.1rem 0 .5rem}.candidate{border-color:#2563eb}.badge{display:inline-block;padding:.2rem .5rem;border-radius:99px;background:#374151;font-size:.82rem}.badge.candidate{background:#14532d;color:#bbf7d0}.badge.no_signal{background:#374151;color:#d1d5db}.kv{display:grid;grid-template-columns:1fr 1fr;gap:.3rem;color:#cbd5e1}.kv b{color:#fff}.actions{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.8rem}pre{white-space:pre-wrap;word-break:break-word;margin:0}.empty{color:#9ca3af;padding:.8rem 0}.danger{color:#fca5a5}.small{font-size:.88rem}.rate-preview{border:1px solid #365174;border-radius:10px;padding:1rem;background:#0f1b30}.rate-preview strong{color:#bfdbfe}.rate-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.6rem;margin-top:.8rem}.rate-option{padding:.7rem;border:1px solid #334155;border-radius:8px;background:#172033}.rate-option b{display:block;color:#fff;font-size:1.05rem;margin-top:.2rem}
</style></head><body><main><h1>Binance Scanner</h1><p class='muted'>Testnet approval workflow. Signals are informational; proposals, approvals, and execution are separate actions.</p>
<section class='toolbar'><strong>Account mode</strong><button id='demoMode' class='primary' onclick="setMode('demo')">Demo Spot</button><button id='liveMode' onclick="setMode('live')">Live Convert</button><span id='selectedMode' class='badge'>DEMO selected</span></section>
<section class='toolbar'><label>Approval token <input id='token' type='password' autocomplete='off' placeholder='Enter locally; never share it' onblur='maybePreviewLiveRate()'></label><button onclick='loadAll()'>Refresh</button><button onclick='loadAccount()'>Check balances</button><span id='mode' class='badge'>Loading status…</span></section>
<div id='status' class='status' role='status'></div>
<section class='card'><h2>Account</h2><div id='balances' class='muted'>Enter your approval token and click “Check balances”.</div></section>
<section id='liveConvertPanel' class='card' style='display:none'><h2>Live Convert</h2><p class='muted small danger'><strong>LIVE FUNDS.</strong> This mode reads your real Spot wallet. Binance Convert requires at least <strong>0.01 USDT</strong>. Use Binance’s native <strong>Limit Convert</strong> page for unattended limit conversions. This app supports analysis, balance checks, live-rate previews, and manual approval only; it will not automatically accept a live Convert quote.</p><div id='liveMarketPrice' class='rate-preview'>Live market price: connecting…</div><div class='toolbar'><label>From <input id='convertFrom' value='USDT' maxlength='16' onchange='invalidateLiveRate()'></label><label>To <input id='convertTo' value='XRP' maxlength='16' onchange='invalidateLiveRate()'></label><label>Amount (minimum 0.01 USDT) <input id='convertAmount' type='number' min='0.01' step='0.01' value='1' onchange='invalidateLiveRate()'></label><button class='primary' onclick='previewConvertRate()'>Get live rate</button><button class='primary' onclick='requestConvertQuote()'>Get quote</button></div><div id='liveRate' class='empty'>Enter your approval token to load the live Binance Convert rate.</div><div class='toolbar'><label>Trigger price (manual tracking only) <input id='limitPrice' type='number' min='0.00000001' step='0.0001' value='' placeholder='Load live rate first' disabled></label><button id='offset1' onclick='setSuggestedLimit(0.01)' disabled>Use 1% offset</button><button id='offset5' onclick='setSuggestedLimit(0.05)' disabled>Use 5% offset</button><button id='offset10' onclick='setSuggestedLimit(0.10)' disabled>Use 10% offset</button><label>Expires days <input id='limitDays' type='number' min='1' max='30' step='1' value='30'></label><button id='createLimitButton' class='primary' onclick='createConvertLimit()' disabled>Create tracking plan</button></div><div id='convertRequests'></div></section>
<section class='card'><h2>Live signals <span class='muted small'>(watcher checks every second; signals refresh every 30 seconds)</span></h2><div id='signals' class='grid'></div></section>
<section class='card'><h2>Pending approvals and executions</h2><div id='proposals'></div></section>
<script>
let account=null;let selectedMode='demo';
const $=id=>document.getElementById(id);
function token(){return $('token').value.trim()}
function setStatus(message,error=false){$('status').textContent=message;$('status').className='status '+(error?'error':'ok')}
function setMode(mode){selectedMode=mode;const live=mode==='live';$('liveConvertPanel').style.display=live?'block':'none';$('demoMode').className=live?'':'primary';$('liveMode').className=live?'warn':'';$('selectedMode').textContent=live?'LIVE selected — real funds':'DEMO selected';if(live){setStatus('Live Convert selected. Loading the live market price and Convert rate.');loadConvertRequests();loadLiveMarketPrice();maybePreviewLiveRate()}else{$('convertRequests').innerHTML='<div class="empty">Demo Spot selected. Live Convert is disabled until you select Live Convert.</div>';setStatus('Demo Spot selected')}}
async function api(path,options={}){const headers=options.headers||{};if(token())headers['X-Approval-Token']=token();if(selectedMode==='live')headers['X-Account-Mode']='live';if(options.body)headers['Content-Type']='application/json';const response=await fetch(path,{...options,headers});let data;try{data=await response.json()}catch{data=await response.text()}if(!response.ok)throw new Error(typeof data==='string'?data:(data.detail||'Request failed'));return data}
async function loadMode(){try{const health=await api('/health/ready');$('mode').textContent=(health.mode||'unknown')+' · trading '+health.trading_enabled;$('mode').className='badge '+(String(health.trading_enabled)==='true'?'candidate':'')}catch(error){$('mode').textContent='Unavailable';setStatus(error.message,true)}}
async function loadAccount(){try{const endpoint=selectedMode==='live'?'/api/v1/account/live-balances':'/api/v1/account/balances';account=await api(endpoint);const rows=(account.balances||[]).map(b=>'<div class="kv"><span>'+b.asset+'</span><b>'+b.free+' free · '+b.locked+' locked</b></div>').join('');$('balances').innerHTML='<div class="small">'+account.account_type+' · can trade: '+account.can_trade+'</div>'+rows;setStatus((selectedMode==='live'?'Live':'Demo')+' account balances loaded')}catch(error){setStatus(error.message,true)}}
async function loadLiveMarketPrice(){try{const result=await fetch('/api/v1/market/price/XRPUSDT?interval=1m&live=true').then(response=>{if(!response.ok)throw new Error('Market price unavailable');return response.json()});$('liveMarketPrice').innerHTML='<strong>Live production market price:</strong> 1 XRP = '+result.price+' USDT <span class="small muted">· updated '+new Date(result.updated_at).toLocaleTimeString()+'</span>'}catch(error){$('liveMarketPrice').innerHTML='<span class="muted">Live market price is connecting…</span>'}}
async function requestConvertQuote(){if(!token()){setStatus('Enter the approval token first',true);return}try{const result=await api('/api/v1/convert/quotes',{method:'POST',body:JSON.stringify({from_asset:$('convertFrom').value,to_asset:$('convertTo').value,from_amount:Number($('convertAmount').value)})});setStatus('Convert quote '+result.request_id+' created. Review it below.');await loadConvertRequests()}catch(error){setStatus(error.message,true)}}
let previewRate=null;
function setPreviewControls(enabled){['limitPrice','offset1','offset5','offset10','createLimitButton'].forEach(id=>$(id).disabled=!enabled)}
function invalidateLiveRate(){previewRate=null;setPreviewControls(false);$('limitPrice').value='';$('liveRate').className='empty';$('liveRate').textContent=token()?'Settings changed. Loading a fresh Binance Convert rate…':'Enter your approval token to load the live Binance Convert rate.';if(selectedMode==='live'&&token())maybePreviewLiveRate()}
function maybePreviewLiveRate(){if(selectedMode==='live'&&token()&&Number($('convertAmount').value)>=0.01)previewConvertRate()}
function updateOffsetLabels(){if(!previewRate)return;const selling=$('convertFrom').value.trim().toUpperCase()==='XRP'&&$('convertTo').value.trim().toUpperCase()==='USDT';[.01,.05,.10].forEach((offset,index)=>{const value=previewRate*(selling?1+offset:1-offset);$('offset'+[1,5,10][index]).textContent='Use '+(offset*100)+'% offset ('+value.toFixed(4)+')'})}
async function previewConvertRate(){if(!token()){setStatus('Enter the approval token first',true);return}setPreviewControls(false);try{const result=await api('/api/v1/convert/preview',{method:'POST',body:JSON.stringify({from_asset:$('convertFrom').value,to_asset:$('convertTo').value,from_amount:Number($('convertAmount').value)})});previewRate=Number(result.effective_rate);const selling=$('convertFrom').value.trim().toUpperCase()==='XRP'&&$('convertTo').value.trim().toUpperCase()==='USDT';const direction=selling?'sell target: offsets above live rate':'buy target: offsets below live rate';$('liveRate').className='rate-preview';$('liveRate').innerHTML='<strong>Live Binance Convert rate</strong><br>1 '+result.to_asset+' = <strong>'+result.effective_rate+' '+result.from_asset+'</strong> · estimated receive <strong>'+result.to_amount+' '+result.to_asset+'</strong><div class="small muted">'+direction+'. Preview only; it does not reserve funds or create an order. Rates expire quickly.</div><div class="rate-options"><div class="rate-option">Live rate<b>'+result.effective_rate+'</b></div><div class="rate-option">1% offset<b>'+ (previewRate*(selling?1.01:.99)).toFixed(4) +'</b></div><div class="rate-option">5% offset<b>'+ (previewRate*(selling?1.05:.95)).toFixed(4) +'</b></div><div class="rate-option">10% offset<b>'+ (previewRate*(selling?1.10:.90)).toFixed(4) +'</b></div></div>';setPreviewControls(true);updateOffsetLabels();setStatus('Live Convert rate loaded.')}catch(error){setStatus(error.message,true)}}
function setSuggestedLimit(offset){if(!previewRate){setStatus('Get the live rate first',true);return}const selling=$('convertFrom').value.trim().toUpperCase()==='XRP'&&$('convertTo').value.trim().toUpperCase()==='USDT';const value=previewRate*(selling?1+offset:1-offset);$('limitPrice').value=value.toFixed(4);setStatus('Trigger price set to '+value.toFixed(4)+(selling?' (sell target)':' (buy target)'))}
async function createConvertLimit(){if(!token()){setStatus('Enter the approval token first',true);return}try{const result=await api('/api/v1/convert/limits',{method:'POST',body:JSON.stringify({from_asset:$('convertFrom').value,to_asset:$('convertTo').value,from_amount:Number($('convertAmount').value),limit_price:Number($('limitPrice').value),expires_in_days:Number($('limitDays').value)})});setStatus('Limit plan '+result.request_id+' is watching the market.');await loadConvertRequests()}catch(error){setStatus(error.message,true)}}
async function quoteConvertLimit(id){if(!token())return;try{const result=await api('/api/v1/convert/limits/'+id+'/quote',{method:'POST'});setStatus('Fresh quote created for limit plan '+id+'. Review it before approval.');await loadConvertRequests()}catch(error){setStatus(error.message,true)}}
async function cancelConvertLimit(id){if(!token()||!window.confirm('Cancel this limit plan?'))return;try{await api('/api/v1/convert/limits/'+id+'/cancel',{method:'POST'});setStatus('Limit plan cancelled.');await loadConvertRequests()}catch(error){setStatus(error.message,true)}}
async function convertAction(id,operation,version){if(!token()){setStatus('Enter the approval token first',true);return}const message=operation==='approve'?'Approve this live conversion quote?':'Accept this live conversion quote and move real funds?';if(!window.confirm(message))return;try{const result=await api('/api/v1/convert/requests/'+id+'/'+operation,{method:'POST',body:JSON.stringify({expected_version:version})});setStatus('Convert '+operation+' completed: '+(result.order_status||result.state));await loadConvertRequests()}catch(error){setStatus(error.message,true)}}
async function armConvertLimit(id){if(!token()||!window.confirm('Arm one automatic live conversion when this trigger is reached?'))return;try{await api('/api/v1/convert/limits/'+id+'/arm',{method:'POST'});setStatus('Plan armed. It may execute one live conversion when the trigger and fresh-quote checks pass.');await loadConvertRequests()}catch(error){setStatus(error.message,true)}}
async function disarmConvertLimit(id){if(!token())return;try{await api('/api/v1/convert/limits/'+id+'/disarm',{method:'POST'});setStatus('Automatic execution disabled.');await loadConvertRequests()}catch(error){setStatus(error.message,true)}}
async function loadConvertRequests(){if(selectedMode!=='live')return;if(!token()){$('convertRequests').innerHTML='<div class="empty">Enter the approval token to view Convert requests.</div>';return}try{const data=await api('/api/v1/convert/requests');$('convertRequests').replaceChildren(...data.map(p=>{const card=document.createElement('div');card.className='card';const q=p.quote||{};const isLimit=Boolean(p.limit_price);const limitValue=isLimit?p.limit_price+' USDT per '+p.to_asset:'Not used — normal quote';const statusValue=p.order_status||((p.state==='quoted'||p.state==='approved')?'Awaiting acceptance':p.state==='watching'&&isLimit?(p.auto_execute?'Armed — waiting for trigger':'Tracking only — manual review after trigger'):'Not submitted');card.innerHTML='<h3>Convert '+p.request_id+' · '+p.from_asset+' → '+p.to_asset+' <span class="badge">'+p.state+'</span></h3><div class="kv"><span>From amount</span><b>'+p.from_amount+' '+p.from_asset+'</b><span>Trigger price</span><b>'+limitValue+'</b><span>Quoted receive</span><b>'+(q.toAmount||'—')+' '+p.to_asset+'</b><span>Quote ID</span><b>'+((p.quote_id||'—'))+'</b><span>Status</span><b>'+statusValue+'</b></div><div class="actions"></div>';const actions=card.querySelector('.actions');if(p.state==='watching'){const armButton=document.createElement('button');armButton.className=p.auto_execute?'warn':'primary';armButton.textContent=p.auto_execute?'Disarm auto-execution':'Arm auto-execution';armButton.onclick=()=>p.auto_execute?disarmConvertLimit(p.request_id):armConvertLimit(p.request_id);actions.appendChild(armButton);const cancelButton=document.createElement('button');cancelButton.textContent='Cancel tracking plan';cancelButton.onclick=()=>cancelConvertLimit(p.request_id);actions.appendChild(cancelButton)}else if(p.state==='triggered'){const quoteButton=document.createElement('button');quoteButton.className='primary';quoteButton.textContent='Get fresh quote';quoteButton.onclick=()=>quoteConvertLimit(p.request_id);actions.appendChild(quoteButton);const cancelButton=document.createElement('button');cancelButton.textContent='Cancel plan';cancelButton.onclick=()=>cancelConvertLimit(p.request_id);actions.appendChild(cancelButton)}else if(p.state==='quoted'){const button=document.createElement('button');button.className='primary';button.textContent='Approve conversion';button.onclick=()=>convertAction(p.request_id,'approve',p.version);actions.appendChild(button)}else if(p.state==='approved'){const button=document.createElement('button');button.className='warn';button.textContent='Accept live conversion';button.onclick=()=>convertAction(p.request_id,'execute',p.version);actions.appendChild(button)}return card}));if(!data.length)$('convertRequests').innerHTML='<div class="empty">No Convert requests.</div>'}catch(error){setStatus(error.message,true)}}
function accountBalance(){const usdt=(account?.balances||[]).find(b=>b.asset==='USDT');return Number(usdt?.free||0)}
async function createProposal(symbol,button){if(!token()){setStatus('Enter the approval token first',true);return}if(!account)await loadAccount();const balance=accountBalance();if(balance<=0){setStatus('No free USDT balance is available',true);return}button.disabled=true;try{const proposal=await api('/api/v1/proposals',{method:'POST',body:JSON.stringify({symbol,interval:'1m',account_balance:balance,current_exposure:0,available_balance:balance,candle_limit:200})});setStatus('Proposal '+proposal.proposal_id+' created. Review it below.');await loadProposals()}catch(error){setStatus(error.message,true)}finally{button.disabled=false}}
async function loadSignals(){try{const data=await api('/api/v1/signals');$('signals').replaceChildren(...data.map(signal=>{const card=document.createElement('div');card.className='signal card '+(signal.status==='candidate'?'candidate':'');const candidate=signal.status==='candidate';card.innerHTML='<h3>'+signal.symbol+' <span class="badge '+signal.status+'">'+signal.status+'</span></h3><div class="kv"><span>Side</span><b>'+(signal.side||'—')+'</b><span>Confidence</span><b>'+((signal.confidence||0)*100).toFixed(1)+'%</b><span>Entry</span><b>'+(signal.entry_price??'—')+'</b><span>Stop</span><b>'+(signal.stop_price??'—')+'</b><span>Target</span><b>'+(signal.target_price??'—')+'</b></div><p class="muted small">'+(signal.rationale||[]).join(' · ')+'</p>';if(candidate){const actions=document.createElement('div');actions.className='actions';const button=document.createElement('button');button.className='primary';button.textContent='Create proposal';button.onclick=()=>createProposal(signal.symbol,button);actions.appendChild(button);card.appendChild(actions)}return card}));if(!data.length)$('signals').innerHTML='<div class="empty">No signals available.</div>'}catch(error){setStatus(error.message,true)}}
async function proposalAction(id,operation,version){if(!token()){setStatus('Enter the approval token first',true);return}const text=operation==='approve'?'Approve this proposal? Review its symbol, quantity, and risk first.':'Submit this approved order to Binance Testnet? This action cannot be undone.';if(!window.confirm(text))return;try{const result=await api('/api/v1/proposals/'+id+'/'+operation,{method:'POST',body:JSON.stringify({expected_version:version})});setStatus(operation==='approve'?'Proposal approved.':'Testnet order submitted or simulated: '+(result.status||''));await loadProposals()}catch(error){setStatus(error.message,true)}}
async function loadProposals(){try{const data=await api('/api/v1/proposals');const container=$('proposals');container.replaceChildren(...data.map(p=>{const card=document.createElement('div');card.className='card';const s=p.signal||{},r=p.risk||{};card.innerHTML='<h3>Proposal '+p.proposal_id+' · '+p.symbol+' <span class="badge">'+p.state+'</span></h3><div class="kv"><span>Signal</span><b>'+((s.side||'')+' '+((s.confidence||0)*100).toFixed(1)+'%')+'</b><span>Entry</span><b>'+(s.entry_price??'—')+'</b><span>Quantity</span><b>'+(r.quantity??'—')+'</b><span>Notional</span><b>'+(r.notional??'—')+' USDT</b><span>Estimated loss</span><b>'+(r.estimated_loss??'—')+' USDT</b><span>Expires</span><b>'+p.expires_at+'</b></div><div class="actions"></div>';const actions=card.querySelector('.actions');const button=document.createElement('button');button.className=p.state==='approved'?'warn':'primary';button.textContent=p.state==='approved'?'Execute Testnet order':'Approve proposal';button.onclick=()=>proposalAction(p.proposal_id,p.state==='approved'?'execute':'approve',p.version);actions.appendChild(button);return card}));if(!data.length)container.innerHTML='<div class="empty">No pending or approved proposals.</div>'}catch(error){setStatus(error.message,true)}}
async function loadAll(){await Promise.all([loadMode(),loadSignals(),loadProposals(),selectedMode==='live'?loadConvertRequests():Promise.resolve()])}
setMode('demo');loadAll();setInterval(()=>{loadMode();loadSignals();loadProposals();if(selectedMode==='live')loadConvertRequests()},30000);setInterval(()=>{if(selectedMode==='live')loadLiveMarketPrice()},1000);
</script></main></body></html>"""


@app.post("/api/v1/proposals/{proposal_id}/approve", tags=["approvals"])
async def approve_trade_proposal(
    proposal_id: int,
    request: ApprovalRequest,
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
) -> dict[str, object]:
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        proposal = await approve_proposal(
            session, proposal_id, operator.operator_id, request.expected_version
        )
    if proposal is None:
        raise HTTPException(status_code=409, detail="proposal is stale, expired, or not pending")
    return {
        "proposal_id": proposal.id,
        "state": proposal.state,
        "version": proposal.version,
        "approved_at": proposal.approved_at,
        "approval_actor": proposal.approval_actor,
        "execution": "available via explicit execute endpoint",
    }


@app.post("/api/v1/proposals/{proposal_id}/execute", tags=["execution"])
async def execute_trade_proposal(
    proposal_id: int,
    request: ExecutionRequest,
    operator: Annotated[AuthenticatedOperator, Depends(require_approval_token)],
) -> dict[str, object]:
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    async with session_factory() as session:
        proposal_result = await session.execute(
            select(TradeProposal).where(TradeProposal.id == proposal_id).with_for_update()
        )
        proposal = proposal_result.scalar_one_or_none()
        if proposal is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        if proposal.state != "approved" or proposal.version != request.expected_version:
            raise HTTPException(
                status_code=409, detail="proposal is not approved or version is stale"
            )
        if proposal.expires_at <= datetime.now(UTC):
            proposal.state = "expired"
            proposal.version += 1
            await record_audit_event(
                session,
                "proposal_expired_at_execution",
                "execution_service",
                {"proposal_id": proposal.id, "operator_id": operator.operator_id},
                str(proposal.id),
            )
            await session.commit()
            raise HTTPException(status_code=409, detail="proposal has expired")

        execution_result = await session.execute(
            select(TradeExecution).where(TradeExecution.proposal_id == proposal_id)
        )
        existing = execution_result.scalar_one_or_none()
        if existing is not None:
            return _execution_response(existing)

        try:
            intent = order_intent_from_proposal(proposal)
        except OrderExecutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if intent.quantity * intent.reference_price > Decimal(str(settings.max_order_notional)):
            raise HTTPException(status_code=422, detail="order exceeds configured notional cap")
        latest_candle_result = await session.execute(
            select(Candle)
            .where(Candle.symbol == proposal.symbol, Candle.interval == proposal.interval)
            .order_by(desc(Candle.open_time))
            .limit(1)
        )
        latest_candle = latest_candle_result.scalar_one_or_none()
        if latest_candle is None:
            raise HTTPException(status_code=409, detail="no current market price is available")
        current_price = Decimal(str(latest_candle.close_price))
        if intent.quantity * current_price > Decimal(str(settings.max_order_notional)):
            raise HTTPException(
                status_code=422, detail="current order notional exceeds configured cap"
            )
        symbol_result = await session.execute(
            select(Symbol).where(Symbol.symbol == proposal.symbol)
        )
        symbol = symbol_result.scalar_one_or_none()
        if symbol is None or not symbol.is_spot_trading_allowed or symbol.status != "TRADING":
            raise HTTPException(status_code=422, detail="symbol is not enabled for spot trading")
        try:
            validate_order_intent(intent, symbol.filters_json)
        except OrderExecutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        record = TradeExecution(
            proposal_id=proposal.id,
            symbol=proposal.symbol,
            client_order_id=intent.client_order_id,
            status="pending",
            simulated=not settings.trading_enabled,
            message="execution requested",
            requested_by=operator.operator_id,
        )
        session.add(record)
        await record_audit_event(
            session,
            "execution_requested",
            "execution_service",
            {
                "proposal_id": proposal.id,
                "client_order_id": intent.client_order_id,
                "operator_id": operator.operator_id,
                "simulated": not settings.trading_enabled,
            },
            str(proposal.id),
        )
        await session.commit()

        try:
            if settings.trading_enabled:
                api_key = settings.binance_api_key
                api_secret = settings.binance_api_secret
                if api_key is None or api_secret is None:
                    raise OrderExecutionError("Binance API credentials are required for trading")
                async with BinanceTradingClient(
                    str(settings.binance_rest_base_url),
                    api_key.get_secret_value(),
                    api_secret.get_secret_value(),
                    enabled=True,
                    timeout_seconds=settings.binance_timeout_seconds,
                ) as executor:
                    result = await executor.execute(intent)
            else:
                result = await DryRunOrderExecutor().execute(intent)
        except OrderExecutionUncertain as exc:
            record.status = "reconciliation_required"
            record.message = str(exc)
            record.completed_at = None
            await record_audit_event(
                session,
                "execution_reconciliation_required",
                "execution_service",
                {"proposal_id": proposal.id, "operator_id": operator.operator_id},
                str(proposal.id),
            )
            await session.commit()
            raise HTTPException(
                status_code=202,
                detail="order outcome is unknown; reconcile before retrying",
            ) from exc
        except OrderExecutionError as exc:
            record.status = "failed"
            record.message = str(exc)
            record.completed_at = datetime.now(UTC)
            await record_audit_event(
                session,
                "execution_failed",
                "execution_service",
                {"proposal_id": proposal.id, "operator_id": operator.operator_id},
                str(proposal.id),
            )
            await session.commit()
            raise HTTPException(status_code=502, detail="order execution failed") from exc

        record.status = result.status
        record.exchange_order_id = result.exchange_order_id
        record.simulated = result.simulated
        record.message = result.message
        record.completed_at = datetime.now(UTC)
        await record_audit_event(
            session,
            "execution_completed",
            "execution_service",
            {
                "proposal_id": proposal.id,
                "client_order_id": result.client_order_id,
                "exchange_order_id": result.exchange_order_id,
                "status": result.status,
                "simulated": result.simulated,
                "operator_id": operator.operator_id,
            },
            str(proposal.id),
        )
        await session.commit()
    return _execution_response(record)


def _execution_response(execution: TradeExecution) -> dict[str, object]:
    return {
        "execution_id": execution.id,
        "proposal_id": execution.proposal_id,
        "symbol": execution.symbol,
        "client_order_id": execution.client_order_id,
        "exchange_order_id": execution.exchange_order_id,
        "status": execution.status,
        "simulated": execution.simulated,
        "message": execution.message,
        "requested_by": execution.requested_by,
        "created_at": execution.created_at,
        "completed_at": execution.completed_at,
    }
