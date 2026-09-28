from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppMode = Literal[
    "backtest",
    "paper",
    "read_only_testnet",
    "approval_testnet",
    "read_only_demo",
    "approval_demo",
]
MarketDataEnvironment = Literal["sandbox", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "binance-market-scanner"
    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_mode: AppMode = "read_only_testnet"
    log_level: str = "INFO"

    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)

    database_url: str = "postgresql+asyncpg://scanner:scanner_dev_password@localhost:5432/scanner"

    binance_rest_base_url: str = "https://testnet.binance.vision"
    binance_ws_base_url: str = "wss://stream.testnet.binance.vision/ws"
    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    live_convert_enabled: bool = False
    live_convert_auto_execution_enabled: bool = False
    live_convert_rest_base_url: str = "https://api.binance.com"
    live_convert_api_key: SecretStr | None = None
    live_convert_api_secret: SecretStr | None = None
    live_convert_allowed_from_assets: str = "USDT"
    live_convert_allowed_to_assets: str = "BTC,ETH"
    live_convert_min_from_amount: float = Field(default=0.01, gt=0, le=1000)
    live_convert_max_from_amount: float = Field(default=10.0, gt=0, le=1000)
    approval_token: SecretStr | None = None
    operator_id: str = Field(default="local-operator", min_length=1, max_length=128)
    mcp_auth_token: SecretStr | None = None
    mcp_auth_mode: Literal["static", "introspection"] = "static"
    mcp_auth_issuer_url: str = "https://auth.example.invalid"
    mcp_resource_url: str = "http://localhost:8000/mcp"
    mcp_required_scope: str = "binance:read"
    mcp_draft_scope: str = "binance:draft"
    mcp_static_scopes: str = "binance:read binance:draft"
    mcp_introspection_url: str | None = None
    mcp_introspection_client_id: str | None = None
    mcp_introspection_client_secret: SecretStr | None = None
    mcp_max_requests_per_minute: int = Field(default=60, ge=1, le=600)

    trading_enabled: bool = False
    withdrawals_enabled: bool = False
    proposal_expiry_seconds: int = Field(default=300, ge=30, le=3600)
    binance_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    market_data_quote_asset: str = "USDT"
    market_data_environment: MarketDataEnvironment = "sandbox"
    ingest_symbols: str = "BTCUSDT,ETHUSDT"
    ingest_interval: str = "1m"
    ingest_backfill_limit: int = Field(default=500, ge=1, le=1000)
    signal_scan_seconds: int = Field(default=1, ge=1, le=3600)
    market_data_max_age_seconds: int = Field(default=10, ge=1, le=3600)
    max_order_notional: float = Field(default=10.0, gt=0, le=1000)

    @property
    def ingest_symbol_list(self) -> list[str]:
        return [
            symbol.strip().upper() for symbol in self.ingest_symbols.split(",") if symbol.strip()
        ]

    @property
    def live_convert_from_asset_list(self) -> list[str]:
        return [
            asset.strip().upper()
            for asset in self.live_convert_allowed_from_assets.split(",")
            if asset.strip()
        ]

    @property
    def live_convert_to_asset_list(self) -> list[str]:
        return [
            asset.strip().upper()
            for asset in self.live_convert_allowed_to_assets.split(",")
            if asset.strip()
        ]

    @property
    def mcp_static_scope_list(self) -> list[str]:
        return [scope.strip() for scope in self.mcp_static_scopes.split() if scope.strip()]

    @model_validator(mode="after")
    def enforce_safety_boundary(self) -> Settings:
        if self.market_data_environment != "sandbox":
            raise ValueError("the configured ingestor may only persist sandbox market data")
        rest_host = (urlparse(str(self.binance_rest_base_url)).hostname or "").lower()
        ws_host = (urlparse(self.binance_ws_base_url).hostname or "").lower()
        if urlparse(str(self.binance_rest_base_url)).scheme != "https":
            raise ValueError("Binance REST endpoints must use HTTPS")
        if urlparse(self.binance_ws_base_url).scheme != "wss":
            raise ValueError("Binance WebSocket endpoints must use WSS")

        sandbox_hosts = {
            "testnet": ("testnet.binance.vision", "stream.testnet.binance.vision"),
            "demo": ("demo-api.binance.com", "demo-stream.binance.com"),
        }
        if self.app_mode in {"read_only_demo", "approval_demo"}:
            expected_hosts = sandbox_hosts["demo"]
            endpoint_name = "Binance Demo"
        else:
            expected_hosts = sandbox_hosts["testnet"]
            endpoint_name = "Binance Testnet"
        if rest_host != expected_hosts[0] or ws_host != expected_hosts[1]:
            raise ValueError(f"{self.app_mode} requires {endpoint_name} endpoints")
        if self.withdrawals_enabled:
            raise ValueError("Withdrawals are permanently disabled by the initial safety boundary")
        if self.trading_enabled and self.app_mode not in {"approval_testnet", "approval_demo"}:
            raise ValueError("Trading can only be enabled in an approval sandbox mode")
        if self.app_mode in {"read_only_testnet", "read_only_demo"} and self.trading_enabled:
            raise ValueError(f"{self.app_mode} cannot enable trading")
        convert_host = (urlparse(str(self.live_convert_rest_base_url)).hostname or "").lower()
        if urlparse(str(self.live_convert_rest_base_url)).scheme != "https":
            raise ValueError("live Convert endpoint must use HTTPS")
        if convert_host != "api.binance.com":
            raise ValueError("live Convert is restricted to api.binance.com")
        if self.live_convert_auto_execution_enabled and not self.live_convert_enabled:
            raise ValueError("automatic live Convert execution requires LIVE_CONVERT_ENABLED=true")
        if self.live_convert_enabled and (
            not self.live_convert_api_key
            or not self.live_convert_api_key.get_secret_value()
            or not self.live_convert_api_secret
            or not self.live_convert_api_secret.get_secret_value()
        ):
            raise ValueError("live Convert requires separate API credentials")
        if self.trading_enabled and (
            not self.binance_api_key
            or not self.binance_api_key.get_secret_value()
            or not self.binance_api_secret
            or not self.binance_api_secret.get_secret_value()
        ):
            raise ValueError("trading requires Binance API credentials")
        self._validate_mcp_authentication()
        return self

    def _validate_mcp_authentication(self) -> None:
        if not self.mcp_required_scope.strip() or not self.mcp_draft_scope.strip():
            raise ValueError("MCP read and draft scopes must both be configured")
        if self.mcp_required_scope == self.mcp_draft_scope:
            raise ValueError("MCP read and draft scopes must be different")

        resource = urlparse(self.mcp_resource_url)
        if resource.scheme not in {"http", "https"} or not resource.netloc:
            raise ValueError("MCP_RESOURCE_URL must be an absolute HTTP(S) URL")
        if resource.query or resource.fragment or resource.path.rstrip("/") != "/mcp":
            raise ValueError(
                "MCP_RESOURCE_URL must be the canonical /mcp URL without query or fragment"
            )

        if self.mcp_auth_mode != "introspection":
            return

        if self.mcp_auth_issuer_url == "https://auth.example.invalid":
            raise ValueError(
                "MCP_AUTH_ISSUER_URL must be set to the OAuth issuer when "
                "MCP_AUTH_MODE=introspection"
            )
        placeholder_values = {
            "MCP_AUTH_ISSUER_URL": self.mcp_auth_issuer_url,
            "MCP_RESOURCE_URL": self.mcp_resource_url,
            "MCP_INTROSPECTION_URL": self.mcp_introspection_url or "",
            "MCP_INTROSPECTION_CLIENT_ID": self.mcp_introspection_client_id or "",
            "MCP_INTROSPECTION_CLIENT_SECRET": (
                self.mcp_introspection_client_secret.get_secret_value()
                if self.mcp_introspection_client_secret is not None
                else ""
            ),
        }
        for name, value in placeholder_values.items():
            if any(marker in value for marker in ("YOUR-", "REPLACE_LOCALLY")):
                raise ValueError(f"{name} still contains an example placeholder")
        issuer = urlparse(self.mcp_auth_issuer_url)
        if issuer.scheme != "https" or not issuer.netloc or issuer.query or issuer.fragment:
            raise ValueError(
                "MCP_AUTH_ISSUER_URL must be an HTTPS issuer URL without query or fragment"
            )
        if not self.mcp_introspection_url:
            raise ValueError("MCP_INTROSPECTION_URL is required when MCP_AUTH_MODE=introspection")
        introspection = urlparse(self.mcp_introspection_url)
        if introspection.scheme != "https" or not introspection.netloc:
            raise ValueError("MCP_INTROSPECTION_URL must be an absolute HTTPS URL")
        if introspection.query or introspection.fragment:
            raise ValueError("MCP_INTROSPECTION_URL must not contain a query or fragment")
        if not self.mcp_introspection_client_id:
            raise ValueError(
                "MCP_INTROSPECTION_CLIENT_ID is required when MCP_AUTH_MODE=introspection"
            )
        if (
            self.mcp_introspection_client_secret is None
            or not self.mcp_introspection_client_secret.get_secret_value()
        ):
            raise ValueError(
                "MCP_INTROSPECTION_CLIENT_SECRET is required when MCP_AUTH_MODE=introspection"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
