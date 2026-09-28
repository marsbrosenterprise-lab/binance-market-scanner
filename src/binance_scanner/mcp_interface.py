from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hmac import compare_digest
from typing import Literal

import httpx
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, BaseModel, Field, field_validator
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from binance_scanner.binance.account import (
    BinanceAccountClient,
    BinanceAccountError,
    non_zero_balances,
)
from binance_scanner.binance.rest import BinanceRestClient, BinanceRestError
from binance_scanner.config import Settings, get_settings
from binance_scanner.database import check_database, create_session_factory
from binance_scanner.indicators import CandlePoint, IndicatorEngine
from binance_scanner.market_data import CandleRecord, parse_rest_kline
from binance_scanner.models import (
    Candle,
    ConvertRequest,
    DraftProposal,
    SafetyControl,
    TradeExecution,
)
from binance_scanner.proposals import record_audit_event

MAX_ITEMS = 100
MAX_CANDLES = 500


class StaticTokenVerifier(TokenVerifier):
    """Small development verifier; production must use the configured OAuth issuer."""

    async def verify_token(self, token: str) -> AccessToken | None:
        configured = get_settings().mcp_auth_token
        if configured is None or not compare_digest(token, configured.get_secret_value()):
            return None
        settings = get_settings()
        return AccessToken(
            token=token,
            client_id="chatgpt-private-mcp",
            scopes=[settings.mcp_required_scope],
            resource=settings.mcp_resource_url,
            subject=settings.operator_id,
        )


class IntrospectionTokenVerifier(TokenVerifier):
    """Verify OAuth bearer tokens through an RFC 7662 introspection endpoint."""

    async def verify_token(self, token: str) -> AccessToken | None:
        settings = get_settings()
        if (
            not settings.mcp_introspection_url
            or not settings.mcp_introspection_client_id
            or settings.mcp_introspection_client_secret is None
        ):
            return None
        try:
            async with httpx.AsyncClient(timeout=settings.binance_timeout_seconds) as client:
                response = await client.post(
                    settings.mcp_introspection_url,
                    data={"token": token},
                    auth=(
                        settings.mcp_introspection_client_id,
                        settings.mcp_introspection_client_secret.get_secret_value(),
                    ),
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("active") is not True:
            return None
        raw_scopes = payload.get("scope", "")
        scopes = raw_scopes.split() if isinstance(raw_scopes, str) else []
        if settings.mcp_required_scope not in scopes:
            return None
        subject = payload.get("sub")
        return AccessToken(
            token=token,
            client_id=str(payload.get("client_id", "chatgpt")),
            scopes=scopes,
            resource=settings.mcp_resource_url,
            subject=subject if isinstance(subject, str) else None,
            claims=payload,
        )


def _auth_settings(settings: Settings) -> AuthSettings:
    return AuthSettings(
        issuer_url=AnyHttpUrl(settings.mcp_auth_issuer_url),
        resource_server_url=AnyHttpUrl(settings.mcp_resource_url),
        required_scopes=[settings.mcp_required_scope],
        validate_token_resource=True,
    )


_settings = get_settings()
mcp = MCPServer(
    "Binance Market Scanner",
    version="0.1.0",
    instructions=(
        "Return source, environment, timestamps, freshness, and units. "
        "Market data is indicative; Convert quotes are executable only through the dashboard. "
        "Never claim continuous monitoring, approval, or execution."
    ),
    token_verifier=(
        IntrospectionTokenVerifier()
        if _settings.mcp_auth_mode == "introspection"
        else StaticTokenVerifier()
    ),
    auth=_auth_settings(_settings),
)
mcp_http_app = mcp.streamable_http_app()

READ_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
WRITE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

_request_times: deque[datetime] = deque()


def _rate_limit() -> None:
    now = datetime.now(UTC)
    settings = get_settings()
    while _request_times and (now - _request_times[0]).total_seconds() >= 60:
        _request_times.popleft()
    if len(_request_times) >= settings.mcp_max_requests_per_minute:
        raise ToolError("MCP request rate limit exceeded")
    _request_times.append(now)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _environment_status(
    environment: str, source_timestamp: datetime | None, retrieved_at: datetime
) -> str:
    if source_timestamp is None:
        return "unavailable"
    age = (retrieved_at - source_timestamp).total_seconds()
    return "fresh" if age <= get_settings().market_data_max_age_seconds else "stale"


async def _candle_snapshot(
    symbol: str, interval: str, environment: Literal["sandbox", "production"]
) -> tuple[list[CandleRecord] | list[Candle], datetime, str]:
    settings = get_settings()
    retrieved_at = datetime.now(UTC)
    if environment == "production":
        async with BinanceRestClient(
            "https://api.binance.com", settings.binance_timeout_seconds
        ) as client:
            rows = await client.get_klines(symbol, interval, MAX_CANDLES)
        records = [parse_rest_kline(symbol, interval, row, "production") for row in rows]
        return records, retrieved_at, "Binance public production REST"
    factory = create_session_factory(settings.database_url)
    async with factory() as session:
        result = await session.execute(
            select(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.interval == interval,
                Candle.environment == "sandbox",
            )
            .order_by(desc(Candle.open_time))
            .limit(MAX_CANDLES)
        )
        return (
            list(reversed(result.scalars().all())),
            retrieved_at,
            "Binance sandbox data persisted by ingestor",
        )


def _record_payload(record: Candle | CandleRecord) -> dict[str, str | bool]:
    if isinstance(record, Candle):
        return {
            "open_time": record.open_time.isoformat(),
            "close_time": record.close_time.isoformat(),
            "open": str(record.open_price),
            "high": str(record.high_price),
            "low": str(record.low_price),
            "close": str(record.close_price),
            "volume": str(record.volume),
            "closed": record.is_closed,
        }
    return {
        "open_time": record.open_time.isoformat(),
        "close_time": record.close_time.isoformat(),
        "open": str(record.open_price),
        "high": str(record.high_price),
        "low": str(record.low_price),
        "close": str(record.close_price),
        "volume": str(record.volume),
        "closed": record.is_closed,
    }


@mcp.tool(title="Get bot status", annotations=READ_ANNOTATIONS)
async def get_bot_status() -> dict[str, object]:
    """Return safe application health, mode, environment, and emergency-stop status."""
    _rate_limit()
    settings = get_settings()
    factory = create_session_factory(settings.database_url)
    async with factory() as session:
        result = await session.execute(select(SafetyControl).where(SafetyControl.id == 1))
        control = result.scalar_one_or_none()
    return {
        "status": "ok" if await check_database(settings.database_url) else "unavailable",
        "app_mode": settings.app_mode,
        "market_data_environment": settings.market_data_environment,
        "trading_enabled": settings.trading_enabled,
        "live_convert_enabled": settings.live_convert_enabled,
        "emergency_stop": bool(control and control.emergency_stop),
        "retrieved_at": datetime.now(UTC).isoformat(),
    }


@mcp.tool(title="Get market snapshot", annotations=READ_ANNOTATIONS)
async def get_market_snapshot(
    symbol: str,
    interval: str = "1m",
    environment: Literal["sandbox", "production"] = "production",
) -> dict[str, object]:
    """Fetch bounded candle data and an indicative price with explicit provenance."""
    _rate_limit()
    normalized = symbol.strip().upper()
    if not normalized or len(normalized) > 32 or not normalized.isalnum():
        raise ToolError("symbol must be an uppercase alphanumeric Binance symbol")
    try:
        records, retrieved_at, source = await _candle_snapshot(normalized, interval, environment)
    except (BinanceRestError, ValueError) as exc:
        raise ToolError("market data is unavailable") from exc
    latest = records[-1] if records else None
    source_timestamp = latest.close_time if latest else None
    return {
        "symbol": normalized,
        "data_source": source,
        "environment": environment,
        "source_timestamp": _iso(source_timestamp),
        "retrieval_timestamp": retrieved_at.isoformat(),
        "freshness_status": _environment_status(environment, source_timestamp, retrieved_at),
        "price": str(latest.close_price) if latest else None,
        "units": {"price": "USDT per base asset", "volume": "base asset"},
        "candles": [_record_payload(record) for record in records[-100:]],
    }


@mcp.tool(title="Get indicators", annotations=READ_ANNOTATIONS)
async def get_indicators(
    symbol: str,
    interval: str = "1m",
    environment: Literal["sandbox", "production"] = "production",
) -> dict[str, object]:
    """Compute indicators from the same bounded, provenance-labelled market snapshot."""
    _rate_limit()
    snapshot = await get_market_snapshot(symbol, interval, environment)
    candles = snapshot.get("candles")
    if not isinstance(candles, list):
        raise ToolError("indicator data is unavailable")
    points = [
        CandlePoint(float(item["high"]), float(item["low"]), float(item["close"]))
        for item in candles
        if isinstance(item, dict)
    ]
    return {
        "symbol": snapshot["symbol"],
        "data_source": snapshot["data_source"],
        "environment": snapshot["environment"],
        "source_timestamp": snapshot["source_timestamp"],
        "retrieval_timestamp": snapshot["retrieval_timestamp"],
        "freshness_status": snapshot["freshness_status"],
        "indicator_units": {
            "ema": "USDT per base asset",
            "rsi": "0-100",
            "atr": "USDT per base asset",
            "bollinger_width": "fraction",
        },
        "indicators": IndicatorEngine().compute(points).values,
    }


@mcp.tool(title="Get account summary", annotations=READ_ANNOTATIONS)
async def get_account_summary() -> dict[str, object]:
    """Read non-zero balances only; credentials never leave the backend."""
    _rate_limit()
    settings = get_settings()
    key, secret = settings.binance_api_key, settings.binance_api_secret
    if key is None or secret is None:
        return {"available": False, "reason": "sandbox account credentials are not configured"}
    try:
        async with BinanceAccountClient(
            str(settings.binance_rest_base_url),
            key.get_secret_value(),
            secret.get_secret_value(),
            timeout_seconds=settings.binance_timeout_seconds,
        ) as client:
            account = await client.get_account()
    except (BinanceAccountError, ValueError) as exc:
        raise ToolError("account summary is unavailable") from exc
    return {
        "available": True,
        "environment": "sandbox",
        "account_type": "SPOT_DEMO"
        if "demo" in str(settings.binance_rest_base_url)
        else "SPOT_TESTNET",
        "can_trade": bool(account.get("canTrade", False)),
        "balances": non_zero_balances(account),
        "retrieval_timestamp": datetime.now(UTC).isoformat(),
    }


def _convert_summary(request: ConvertRequest) -> dict[str, object]:
    return {
        "request_id": request.id,
        "state": request.state,
        "from_asset": request.from_asset,
        "to_asset": request.to_asset,
        "from_amount": str(request.from_amount),
        "limit_price": str(request.limit_price) if request.limit_price is not None else None,
        "auto_execute": request.auto_execute,
        "expires_at": _iso(request.expires_at),
        "triggered_at": _iso(request.triggered_at),
        "order_status": request.order_status,
        "created_at": _iso(request.created_at),
    }


@mcp.tool(title="List active plans", annotations=READ_ANNOTATIONS)
async def list_active_plans() -> list[dict[str, object]]:
    """List active Convert plans without exposing quotes, credentials, or execution controls."""
    _rate_limit()
    factory = create_session_factory(get_settings().database_url)
    async with factory() as session:
        result = await session.execute(
            select(ConvertRequest)
            .where(ConvertRequest.state.in_(["watching", "triggered", "quoted", "approved"]))
            .order_by(desc(ConvertRequest.created_at))
            .limit(MAX_ITEMS)
        )
        return [_convert_summary(item) for item in result.scalars().all()]


@mcp.tool(title="Get execution status", annotations=READ_ANNOTATIONS)
async def get_execution_status() -> list[dict[str, object]]:
    """List unresolved execution ledger rows; this tool cannot retry or execute them."""
    _rate_limit()
    factory = create_session_factory(get_settings().database_url)
    async with factory() as session:
        result = await session.execute(
            select(TradeExecution)
            .where(TradeExecution.status.in_(["pending", "processing", "reconciliation_required"]))
            .order_by(desc(TradeExecution.created_at))
            .limit(MAX_ITEMS)
        )
        return [
            {
                "execution_id": row.id,
                "proposal_id": row.proposal_id,
                "symbol": row.symbol,
                "status": row.status,
                "simulated": row.simulated,
                "message": row.message,
                "created_at": _iso(row.created_at),
            }
            for row in result.scalars().all()
        ]


@mcp.tool(title="Get trade history", annotations=READ_ANNOTATIONS)
async def get_trade_history(limit: int = 50) -> list[dict[str, object]]:
    """Return bounded execution history without exchange-sensitive details."""
    _rate_limit()
    limit = max(1, min(limit, MAX_ITEMS))
    factory = create_session_factory(get_settings().database_url)
    async with factory() as session:
        result = await session.execute(
            select(TradeExecution).order_by(desc(TradeExecution.created_at)).limit(limit)
        )
        return [
            {
                "execution_id": row.id,
                "proposal_id": row.proposal_id,
                "symbol": row.symbol,
                "status": row.status,
                "simulated": row.simulated,
                "message": row.message,
                "created_at": _iso(row.created_at),
                "completed_at": _iso(row.completed_at),
            }
            for row in result.scalars().all()
        ]


class DraftInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    action: Literal["BUY", "SELL", "HOLD", "NO_TRADE"]
    amount: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    amount_asset: str = Field(min_length=2, max_length=16)
    entry_condition: str = Field(min_length=1, max_length=500)
    expires_in_hours: int = Field(default=24, ge=1, le=720)
    exit_conditions: list[str] = Field(min_length=1, max_length=10)
    reasoning: str = Field(min_length=1, max_length=2000)
    uncertainty: str = Field(min_length=1, max_length=1000)
    snapshot_refs: list[str] = Field(min_length=1, max_length=20)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("symbol", "amount_asset", mode="before")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        return value.strip().upper()


def _draft_payload(draft: DraftProposal) -> dict[str, object]:
    return {
        "draft_id": draft.id,
        "symbol": draft.symbol,
        "action": draft.action,
        "amount": str(draft.amount),
        "amount_asset": draft.amount_asset,
        "entry_condition": draft.entry_condition,
        "expires_at": draft.expires_at.isoformat(),
        "exit_conditions": json.loads(draft.exit_conditions_json),
        "reasoning": draft.reasoning,
        "uncertainty": draft.uncertainty,
        "snapshot_refs": json.loads(draft.snapshot_refs_json),
        "state": draft.state,
        "origin": draft.origin,
        "schema_version": draft.schema_version,
        "created_at": _iso(draft.created_at),
        "execution": "not available; dashboard approval is required",
    }


@mcp.tool(title="Create draft proposal", annotations=WRITE_ANNOTATIONS)
async def create_draft_proposal(draft: DraftInput) -> dict[str, object]:
    """Save an unapproved analysis draft; it cannot approve, arm, reserve, or execute funds."""
    _rate_limit()
    now = datetime.now(UTC)
    factory = create_session_factory(get_settings().database_url)
    async with factory() as session:
        existing_result = await session.execute(
            select(DraftProposal).where(DraftProposal.idempotency_key == draft.idempotency_key)
        )
        existing = existing_result.scalar_one_or_none()
        if existing is not None:
            return _draft_payload(existing)
        record = DraftProposal(
            symbol=draft.symbol,
            action=draft.action,
            amount=draft.amount,
            amount_asset=draft.amount_asset,
            entry_condition=draft.entry_condition,
            expires_at=now + timedelta(hours=draft.expires_in_hours),
            exit_conditions_json=json.dumps(draft.exit_conditions, separators=(",", ":")),
            reasoning=draft.reasoning,
            uncertainty=draft.uncertainty,
            snapshot_refs_json=json.dumps(draft.snapshot_refs, separators=(",", ":")),
            idempotency_key=draft.idempotency_key,
            origin="mcp",
            schema_version="1",
            state="unapproved",
        )
        session.add(record)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise ToolError("draft idempotency key is already in use") from exc
        await record_audit_event(
            session,
            "mcp_draft_created",
            "mcp",
            {"draft_id": record.id, "symbol": record.symbol, "action": record.action},
            str(record.id),
        )
        await session.commit()
        await session.refresh(record)
        return _draft_payload(record)


@mcp.tool(title="Get draft proposal", annotations=READ_ANNOTATIONS)
async def get_draft_proposal(draft_id: int) -> dict[str, object]:
    """Retrieve one unapproved or reviewed draft for human review in the dashboard."""
    _rate_limit()
    factory = create_session_factory(get_settings().database_url)
    async with factory() as session:
        result = await session.execute(select(DraftProposal).where(DraftProposal.id == draft_id))
        draft = result.scalar_one_or_none()
    if draft is None:
        raise ToolError("draft proposal not found")
    return _draft_payload(draft)
