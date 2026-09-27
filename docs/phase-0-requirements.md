# Binance Market Scanner and Approval-Based Trading Bot

## Phase 0: Requirements and Safety Boundary

Status: Draft baseline for implementation review  
Date: 2026-09-26  
Scope: Binance Spot, Testnet first

## 1. Objective

Build a continuously running Binance Spot market scanner that produces explainable trading opportunities, applies configurable risk controls, and requires explicit authenticated user approval before any order can be submitted.

The first implementation must support read-only scanning, paper evaluation, backtesting, and Binance Spot Testnet execution. Production/live trading is out of scope until a separate review explicitly enables it.

## 2. Initial scope

### Included

- Binance Spot markets only.
- Testnet as the only executable environment.
- Public market-data ingestion and normalization.
- Modular technical indicators and strategy plugins.
- Explainable opportunity signals.
- Position sizing and portfolio-level risk checks.
- Persisted trade proposals and approval decisions.
- Explicit approval before order submission.
- Testnet order execution and reconciliation.
- PostgreSQL-backed operational records and audit events.
- Dashboard for scanner results, approvals, risk state, orders, fills, and audit history.
- Backtesting and paper-trading paths using the same strategy and risk modules.

### Excluded from the first release

- Futures, margin, options, leveraged products, and copy trading.
- Withdrawals, deposits, transfers, or wallet automation.
- Automatic trading without approval.
- Machine-learning strategies.
- Tax, accounting, or regulatory reporting.
- Multiple exchanges.
- Mobile applications.
- Public multi-user hosting.

## 3. Operating modes

The system must have an explicit mode, visible in the dashboard and logs:

| Mode | Market data | Signal generation | Order submission |
|---|---|---|---|
| Backtest | Historical | Yes | Never |
| Paper | Live or replayed | Yes | Never |
| Read-only Testnet | Testnet/public | Yes | Disabled |
| Approval Testnet | Testnet | Yes | Only after approval |
| Live | Production | Not enabled in Phase 0 | Forbidden in initial release |

The application must fail closed if the configured mode is missing, invalid, or inconsistent with the configured Binance endpoint.

## 4. Default trading assumptions

These are conservative implementation defaults and require review before execution is enabled:

- Spot-only, long-only trading.
- One base quote currency initially, preferably USDT, subject to Testnet symbol availability.
- Limit orders initially; market orders require a later decision and separate slippage controls.
- One active position per symbol.
- No borrowing and no margin.
- No order submitted if price, balance, symbol filters, or risk data is stale.
- Every proposal expires after a short configurable period, initially 5 minutes.
- Approval is bound to the exact symbol, side, quantity, price constraints, stop, target, strategy version, and risk assessment shown to the user.
- Any material change invalidates the approval and requires a new proposal.

## 5. Approval boundary

No signal, strategy, scheduled job, retry loop, or recovery process may submit an order directly.

Required workflow:

1. A scanner produces a candidate.
2. The strategy creates an explainable signal.
3. The risk engine accepts or rejects the signal.
4. An immutable trade proposal is persisted.
5. The authenticated user explicitly approves or rejects that proposal.
6. The system revalidates market data, balances, filters, exposure, signal age, and risk limits.
7. Only a valid, unexpired approval may reach the execution adapter.
8. The submitted order and all exchange responses are reconciled and audited.

Approval actions must be single-use, authenticated, timestamped, and associated with the user identity and proposal version.

## 6. Risk-control requirements

The risk layer must support configurable hard limits for:

- Maximum risk per trade.
- Maximum notional per order.
- Maximum total portfolio exposure.
- Maximum exposure per symbol and quote currency.
- Maximum number of open positions.
- Maximum daily loss.
- Maximum consecutive losses.
- Minimum available balance reserve.
- Maximum spread and estimated slippage.
- Minimum quote volume and liquidity.
- Maximum signal age.
- Maximum volatility or ATR-based distance.
- Maximum order-count and request-rate budgets.

Any hard-limit violation must prevent proposal approval or order submission. The reason must be visible and recorded.

The system must provide a global kill switch that prevents new orders while preserving market-data ingestion, reconciliation, and audit logging.

## 7. Security boundary

- Testnet and production credentials must be separate credentials and separate configuration profiles.
- API keys must have trading permission only; withdrawal permission must be disabled.
- Secrets must be supplied through a secret manager or protected environment configuration, never committed to Git or stored in the database.
- Secret values must be redacted from logs, exceptions, traces, and dashboard responses.
- The execution service must have the narrowest network and filesystem permissions practical.
- Dashboard actions must require authentication and authorization.
- Approval endpoints must defend against replay, duplicate submission, CSRF where applicable, and stale proposals.
- Production endpoints and credentials must be rejected by the initial release at configuration validation time.
- Every order request must have a correlation ID and idempotent client order ID.

## 8. Audit requirements

Audit records must be append-only from the application perspective and include:

- Event ID and correlation ID.
- Event type and timestamp in UTC.
- Actor or system component.
- Operating mode and strategy version.
- Symbol and proposal/order identifiers where relevant.
- Input and output summaries.
- Risk decisions and rejected-rule details.
- Approval or rejection decision, user identity, and timestamp.
- Binance request metadata, response status, and exchange order identifiers.
- Hash or equivalent integrity field for important payloads.

Audit logging must cover startup, configuration changes, data-quality incidents, signal creation, proposal creation, approval, rejection, expiration, kill-switch changes, order submission, retries, reconciliation, and shutdown/recovery.

## 9. Data and persistence requirements

Operational persistence must support:

- Symbols and exchange filters.
- Normalized candles and market snapshots.
- Indicator snapshots and strategy signals.
- Trade proposals and approval history.
- Orders, fills, balances, positions, and reconciliation results.
- Risk-limit configuration versions.
- Backtest runs and result summaries.
- Audit events and system health events.

All timestamps must be stored in UTC. Exchange timestamps and local receipt timestamps must both be retained where useful for latency and data-quality analysis.

## 10. Backtesting and testing requirements

The same indicator, strategy, and risk interfaces must be usable in:

- Historical backtests.
- Deterministic replay tests.
- Paper trading.
- Testnet execution.

Backtests must account for fees, spread, slippage assumptions, candle timing, partial fills, symbol filters, position limits, and look-ahead bias.

Minimum test categories:

- Indicator and position-sizing unit tests.
- Risk-invariant and property tests.
- Binance adapter contract tests.
- WebSocket disconnect and replay tests.
- Approval replay and stale-proposal tests.
- Duplicate-order and unknown-execution-status tests.
- Database migration and recovery tests.
- End-to-end proposal-to-reconciliation tests.

## 11. Initial dashboard requirements

The first dashboard must show:

- Current operating mode and connectivity health.
- Scanner candidates with strategy and indicator explanations.
- Pending, approved, rejected, and expired proposals.
- Exact order preview before approval.
- Current risk limits and utilization.
- Orders, fills, balances, and positions.
- P&L and backtest summaries.
- Kill-switch state.
- Searchable audit events.

The UI must clearly distinguish a signal, a proposal, an approval, a submitted order, and an exchange-confirmed fill.

## 12. Phase 0 acceptance criteria

Phase 0 is complete when:

- The scope and exclusions above are accepted as the initial product boundary.
- Testnet is the only executable environment.
- The approval workflow and fail-closed rules are agreed.
- Risk limits have named configuration owners and initial values.
- Credential, secret, and network boundaries are agreed.
- Audit events and retention expectations are agreed.
- The first strategy and supported timeframes are selected.
- The first dashboard and backtesting milestone are agreed.
- Any deviations from these defaults are recorded as explicit decisions.

## 13. Decisions requiring confirmation before Phase 1

The following defaults will be used unless changed:

1. Python/FastAPI backend with PostgreSQL and Docker Compose.
2. React-based dashboard, with a minimal first version.
3. USDT-quoted Spot Testnet markets as the initial universe.
4. Limit orders only for the first execution milestone.
5. Long-only, one-position-per-symbol behavior.
6. One explainable trend-plus-momentum strategy for the first end-to-end path.
7. Five-minute proposal expiration.
8. No live-trading code path enabled in the initial release.

