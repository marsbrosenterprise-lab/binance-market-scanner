from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import SecretStr

from binance_scanner.config import Settings
from binance_scanner.mcp_interface import (
    DraftInput,
    IntrospectionTokenVerifier,
    StaticTokenVerifier,
    _require_scope,
    mcp,
)


def test_mcp_exposes_only_scoped_tools() -> None:
    names = {tool.name for tool in mcp._tool_manager.list_tools()}

    assert names == {
        "get_bot_status",
        "get_market_snapshot",
        "get_indicators",
        "get_account_summary",
        "list_active_plans",
        "get_execution_status",
        "get_trade_history",
        "create_draft_proposal",
        "get_draft_proposal",
    }


def test_draft_input_normalizes_and_bounds_payload() -> None:
    draft = DraftInput(
        symbol="xrpusdt",
        action="BUY",
        amount="0.01000000",
        amount_asset="usdt",
        entry_condition="price at or below 1.50 USDT/XRP",
        exit_conditions=["review at 1.60"],
        reasoning="Indicative momentum setup.",
        uncertainty="Convert quote can differ from ticker price.",
        snapshot_refs=["snapshot:2026-09-28T00:00:00Z"],
        idempotency_key="draft-test-0001",
    )

    assert draft.symbol == "XRPUSDT"
    assert draft.amount_asset == "USDT"


@pytest.mark.asyncio
async def test_static_verifier_rejects_missing_or_wrong_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, mcp_auth_token=SecretStr("dedicated-token"))
    monkeypatch.setattr("binance_scanner.mcp_interface.get_settings", lambda: settings)
    verifier = StaticTokenVerifier()

    assert await verifier.verify_token("wrong") is None
    accepted = await verifier.verify_token("dedicated-token")
    assert accepted is not None
    assert accepted.scopes == ["binance:read", "binance:draft"]
    assert accepted.resource == settings.mcp_resource_url


def test_tool_scope_boundary_rejects_read_only_token(monkeypatch: pytest.MonkeyPatch) -> None:
    read_only = AccessToken(
        token="read-only",
        client_id="test-client",
        scopes=["binance:read"],
        resource="http://localhost:8000/mcp",
    )
    monkeypatch.setattr("binance_scanner.mcp_interface.get_access_token", lambda: read_only)

    assert _require_scope("binance:read") == read_only
    with pytest.raises(ToolError, match="binance:draft"):
        _require_scope("binance:draft")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_changes",
    [
        {"active": False},
        {"exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())},
        {"iss": "https://wrong.example.test"},
        {"aud": "https://wrong.example.test/mcp"},
        {"scope": "binance:draft"},
    ],
)
async def test_introspection_verifier_fails_closed(
    monkeypatch: pytest.MonkeyPatch, claim_changes: dict[str, object]
) -> None:
    settings = Settings(
        _env_file=None,
        mcp_auth_mode="introspection",
        mcp_auth_issuer_url="https://issuer.example.test",
        mcp_resource_url="https://scanner.example.test/mcp",
        mcp_introspection_url="https://issuer.example.test/introspect",
        mcp_introspection_client_id="resource-client",
        mcp_introspection_client_secret=SecretStr("resource-secret"),
    )
    payload: dict[str, object] = {
        "active": True,
        "iss": settings.mcp_auth_issuer_url,
        "aud": settings.mcp_resource_url,
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        "scope": "binance:read binance:draft",
        "sub": "operator-1",
    }
    payload.update(claim_changes)

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("binance_scanner.mcp_interface.get_settings", lambda: settings)
    monkeypatch.setattr("binance_scanner.mcp_interface.httpx.AsyncClient", FakeClient)

    assert await IntrospectionTokenVerifier().verify_token("opaque-token") is None


@pytest.mark.asyncio
async def test_introspection_verifier_preserves_valid_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        mcp_auth_mode="introspection",
        mcp_auth_issuer_url="https://issuer.example.test",
        mcp_resource_url="https://scanner.example.test/mcp",
        mcp_introspection_url="https://issuer.example.test/introspect",
        mcp_introspection_client_id="resource-client",
        mcp_introspection_client_secret=SecretStr("resource-secret"),
    )
    payload = {
        "active": True,
        "iss": settings.mcp_auth_issuer_url,
        "resource": settings.mcp_resource_url,
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        "scope": "binance:read binance:draft",
        "sub": "operator-1",
    }

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("binance_scanner.mcp_interface.get_settings", lambda: settings)
    monkeypatch.setattr("binance_scanner.mcp_interface.httpx.AsyncClient", FakeClient)

    accepted = await IntrospectionTokenVerifier().verify_token("opaque-token")
    assert accepted is not None
    assert accepted.scopes == ["binance:read", "binance:draft"]
    assert accepted.expires_at == payload["exp"]
