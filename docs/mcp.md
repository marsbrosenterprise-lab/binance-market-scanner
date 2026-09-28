# Private ChatGPT MCP connection

The scanner exposes a Streamable HTTP MCP endpoint at `/mcp` inside the
existing FastAPI service. It uses the official MCP Python SDK (`mcp>=2,<3`),
does not call an OpenAI model API, and reuses the scanner’s database and
Binance services.

## Exposed tools

Read-only tools:

- `get_bot_status`
- `get_market_snapshot`
- `get_indicators`
- `get_account_summary`
- `list_active_plans`
- `get_execution_status`
- `get_trade_history`
- `get_draft_proposal`

The only write tool is `create_draft_proposal`. It saves an `unapproved`
record with an idempotency key. It cannot approve, arm, reserve, or execute
anything. MCP has no tool for trades, Convert quote acceptance, credentials,
limits, emergency-stop changes, SQL, shell commands, or arbitrary URLs.

Every market result identifies the symbol, source, environment, source and
retrieval timestamps, freshness, and units. Prices are indicative market data,
not executable Convert quotes. Missing or stale data is reported explicitly.

## Authentication

Streamable HTTP MCP uses OAuth 2.1 bearer-token semantics. The server is a
resource server: it verifies a bearer token on every MCP request and never
issues tokens. Configure a dedicated identity/provider and least-privilege
scope (`binance:read` by default). Do not reuse `APPROVAL_TOKEN`, Binance
keys, or a dashboard session token.

The current implementation includes a small static-token verifier for local
development and an RFC 7662 introspection verifier for a real provider. Static
mode is fail-closed when `MCP_AUTH_TOKEN` is blank. For ChatGPT, set
`MCP_AUTH_MODE=introspection`, configure the issuer, exact public HTTPS
`MCP_RESOURCE_URL`, introspection URL, and a dedicated introspection client
identity. The issuer must support refresh tokens/offline access for unattended
reconnection. The server then verifies active tokens and the required scope on
every request; it never logs or returns the token.

## Local test

1. Run migrations and the API using `.env.example` plus a local database.
2. Set a random, dedicated `MCP_AUTH_TOKEN` in a protected environment. Never
   commit it.
3. Set `MCP_RESOURCE_URL=http://localhost:8000/mcp` for local MCP clients.
4. Use the MCP Inspector or an SDK test client against `http://localhost:8000/mcp`.

ChatGPT cannot directly reach localhost. For ChatGPT, use a supported Secure
MCP Tunnel or an HTTPS deployment that you control; do not open a public
tunnel without explicit authorization. In ChatGPT developer mode, create a
private custom app, enter the exact HTTPS `/mcp` URL, choose OAuth, complete
authorization, scan the tools, and review the write-action permission before
enabling it. Full write-capable custom MCP apps are workspace-plan dependent.

## Safe usage guidance

Retrieve fresh data before analysis, explain uncertainty, and never promise
returns. Do not describe a draft as approved or executed. ChatGPT does not
continuously monitor markets after a conversation ends. Human approval and
all execution/risk limits remain in the backend dashboard workflow.
