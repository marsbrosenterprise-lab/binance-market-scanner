from __future__ import annotations

import pytest
from pydantic import ValidationError

from binance_scanner.config import Settings


def test_defaults_are_read_only_testnet() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_mode == "read_only_testnet"
    assert settings.trading_enabled is False
    assert settings.withdrawals_enabled is False


def test_production_endpoint_is_rejected() -> None:
    with pytest.raises(ValidationError, match="requires"):
        Settings(_env_file=None, binance_rest_base_url="https://api.binance.com")


def test_trading_requires_approval_testnet_mode() -> None:
    with pytest.raises(ValidationError, match="approval sandbox"):
        Settings(_env_file=None, trading_enabled=True)


def test_enabled_testnet_trading_requires_credentials() -> None:
    with pytest.raises(ValidationError, match="API credentials"):
        Settings(
            _env_file=None,
            app_mode="approval_testnet",
            trading_enabled=True,
            binance_api_key="",
            binance_api_secret="",
        )


def test_demo_mode_accepts_demo_endpoints() -> None:
    settings = Settings(
        _env_file=None,
        app_mode="approval_demo",
        binance_rest_base_url="https://demo-api.binance.com",
        binance_ws_base_url="wss://demo-stream.binance.com/ws",
    )

    assert settings.app_mode == "approval_demo"
    assert settings.trading_enabled is False


def test_automatic_convert_requires_both_live_flags() -> None:
    with pytest.raises(ValidationError, match="LIVE_CONVERT_ENABLED"):
        Settings(_env_file=None, live_convert_auto_execution_enabled=True)


def test_signed_endpoints_require_secure_schemes() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(_env_file=None, binance_rest_base_url="http://testnet.binance.vision")
    with pytest.raises(ValidationError, match="WSS"):
        Settings(_env_file=None, binance_ws_base_url="ws://stream.testnet.binance.vision/ws")
