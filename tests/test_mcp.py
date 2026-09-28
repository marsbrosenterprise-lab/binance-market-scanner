from __future__ import annotations

import pytest
from pydantic import SecretStr

from binance_scanner.config import Settings
from binance_scanner.mcp_interface import DraftInput, StaticTokenVerifier, mcp


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
    assert accepted.scopes == ["binance:read"]
    assert accepted.resource == settings.mcp_resource_url
