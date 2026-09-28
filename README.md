# Binance Market Scanner

Production-oriented foundation for a Binance Spot market scanner and approval-based Testnet trading system.

## Current status

Phase 2 is in progress: repository structure, configuration validation, structured logging, health endpoints, Docker Compose development infrastructure, market-data migrations, Binance Demo connectivity, approval-based Spot execution, and separately gated live Convert support are present.

Historical candle parsing/backfill and live kline persistence are now available as service modules. Strategies, approvals, order execution, and live trading are not implemented.

The read-only ingestion worker can be started with `docker compose up -d ingestor`. It backfills the configured symbols and then listens to Binance Testnet kline streams. Configure `INGEST_SYMBOLS`, `INGEST_INTERVAL`, and `INGEST_BACKFILL_LIMIT` in `.env`.

Persisted indicators are available from `GET /api/v1/market/indicators/{symbol}`. The current read-only engine calculates EMA, RSI, ATR, and Bollinger width from stored candles.

The first explainable signal is available from `GET /api/v1/signals/{symbol}`. It returns a candidate or `no_signal` result; it never submits orders or creates approvals.

The risk engine evaluates candidates independently of execution. It applies per-trade risk, notional, portfolio-exposure, and balance-reserve limits, returning an auditable accept/reject assessment without placing orders.

Trade proposals are persisted with signal and risk snapshots. Proposal creation and approval require `APPROVAL_TOKEN`; approval only changes the proposal state to `approved`. Execution requires a separate authenticated request and is recorded in the execution ledger.
The configured `OPERATOR_ID` is recorded in proposal audit events; approval tokens are never logged.

The private MCP interface is available at `/mcp` when the API is running. It
provides bounded, provenance-labelled market/account/status reads and saves
unapproved analysis drafts. It has no trade, Convert acceptance, approval,
arming, credential, safety-control, SQL, shell, or arbitrary-URL tool. See
[docs/mcp.md](docs/mcp.md) for the OAuth/resource-server design and ChatGPT
connection steps.
The execution boundary supports a deterministic dry-run adapter and a separately gated Binance Spot Testnet adapter. The current default remains dry-run and never submits an exchange request.

Live Convert uses separate credentials and is restricted to `api.binance.com`. Instant conversions follow quote → review → approval → accept, with a minimum from amount of 0.01 USDT. XRP trigger plans are application-managed and are not Binance-native limit orders. In live mode, the one-second monitor reads Binance's production public ticker; sandbox candles remain separate for demo/testnet analysis. A plan is manual by default. An operator can explicitly arm one plan for one-shot automatic execution; the monitor atomically claims it, requests a fresh quote, verifies that its effective rate satisfies the trigger, accepts it once, and reconciles the resulting order status. Pending, failed, and unknown outcomes are retained for recovery; an unknown outcome is never resubmitted automatically. Plans can be cancelled, disarmed, expire, and all lifecycle events are audited.

Offline backtests can be run with `python -m binance_scanner backtest --csv candles.csv --symbol BTCUSDT`. The CSV format is strictly `timestamp,open,high,low,close` with timezone-aware, strictly increasing timestamps.

## Safety boundary

- The configured mode defaults to `read_only_testnet`.
- `TRADING_ENABLED` defaults to `false`.
- Production Binance endpoints are rejected by configuration validation.
- Withdrawal capability is disabled by configuration and is not implemented.
- Live Convert is disabled unless explicitly enabled with separate credentials and an allowlist.
- Limit Convert plans are never automatically armed. Automatic execution requires the dashboard approval token and an explicit per-plan **Arm auto-execution** action; the local configuration flag is disabled by default.
- Proposal risk checks use server-side Binance balances and the execution ledger when credentials are configured; browser-supplied balances are only accepted in explicit `paper`/`backtest` modes.
- Signal stop/target values are informational only; the application does not place automatic exit orders.
- A persistent emergency stop is available through the authenticated safety endpoint and is checked before arming or executing work.
- Credentials are not committed or required for the Phase 1 health checks.

See [docs/phase-0-requirements.md](docs/phase-0-requirements.md) for the approved scope and safety requirements.

## Local setup

1. Copy `.env.example` to `.env` and adjust only local development values.
2. Start PostgreSQL with `docker compose up -d postgres`.
3. Install the project and development dependencies with `python -m pip install -e ".[dev]"`.
4. Run the API with `python -m binance_scanner`.
5. Check `http://localhost:8000/health/live` and `http://localhost:8000/health/ready`.

Docker must be running before starting the PostgreSQL service.

## Checks

```text
ruff check .
ruff format --check .
mypy src
pytest
```

GitHub Actions also provisions an isolated PostgreSQL service, applies all Alembic migrations, and runs the same checks on pushes and pull requests.

## Not implemented yet

Grid/ladder buying, position tracking, realized and unrealized P&L, reserved-balance accounting across multiple plans, and automatic profit-taking remain outside this reliability phase. Spot execution remains sandbox-only, and unresolved Spot submissions are marked for reconciliation using their client order ID before any retry is considered.
