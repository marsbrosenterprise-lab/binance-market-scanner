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

Read tools require `binance:read`. `create_draft_proposal` additionally
requires the separate `binance:draft` scope, enforced in the handler rather
than relying on tool annotations. A draft is idempotent and remains
`unapproved`; it cannot approve, arm, reserve funds, execute trades, accept a
Convert quote, or change safety controls. `get_draft_proposal` is read-only.

Introspection fails closed unless the response is active, issuer-matched,
unexpired, not-before-valid, resource/audience-matched, and scoped. Revocation
is observed on the next introspection request.

## Local test

1. Run migrations and the API using `.env.example` plus a local database.
2. Set a random, dedicated `MCP_AUTH_TOKEN` in a protected environment. Never
   commit it.
3. Set `MCP_RESOURCE_URL=http://localhost:8000/mcp` for local MCP clients.
4. Use the MCP Inspector or an SDK test client against `http://localhost:8000/mcp`.

Use `/mcp` exactly; `/mcp/` is not the configured resource path. The SDK also
serves protected-resource metadata at
`/.well-known/oauth-protected-resource/mcp`. Local verification must cover an
unauthenticated bearer challenge, MCP `initialize`, `tools/list`, and a
harmless read-only `tools/call`. The FastAPI lifespan enters the MCP session
manager during these requests.

## Production provider inputs

No OAuth provider, hosting provider, public tunnel, account, or callback has
been selected or authorized. This service is a resource server; it does not
implement an authorization server, login page, dynamic client registration,
or an improvised token issuer. The selected provider must support Authorization
Code with PKCE (S256), protected-resource and authorization-server discovery,
refresh/offline access, and resource-server validation such as RFC 7662
introspection. It must issue and validate `binance:read` and `binance:draft`,
with issuer, audience/resource, expiry, revocation, and scope claims.

Production configuration template:

```dotenv
MCP_AUTH_MODE=introspection
MCP_AUTH_ISSUER_URL=https://YOUR-ISSUER.example
MCP_RESOURCE_URL=https://YOUR-HOST.example/mcp
MCP_INTROSPECTION_URL=https://YOUR-ISSUER.example/oauth2/introspect
MCP_INTROSPECTION_CLIENT_ID=YOUR_RESOURCE_SERVER_CLIENT_ID
MCP_INTROSPECTION_CLIENT_SECRET=YOUR_RESOURCE_SERVER_CLIENT_SECRET
MCP_REQUIRED_SCOPE=binance:read
MCP_DRAFT_SCOPE=binance:draft
```

The remaining external inputs are the issuer URL, public HTTPS resource URL,
introspection URL, resource-server client ID/secret, selected provider, and
the exact ChatGPT redirect URI shown by ChatGPT’s developer-mode connection
dialog. Do not reuse `APPROVAL_TOKEN`, Binance keys, dashboard cookies, or the
local static token. ChatGPT cannot reach `localhost` directly, and a local SDK
test is not a ChatGPT connection.

For a Keycloak realm named `binance`, the exact endpoint shapes are:

| Purpose | URL |
| --- | --- |
| OAuth issuer | `https://AUTH_HOST/realms/binance` |
| OIDC discovery | `https://AUTH_HOST/realms/binance/.well-known/openid-configuration` |
| Authorization | `https://AUTH_HOST/realms/binance/protocol/openid-connect/auth` |
| Token | `https://AUTH_HOST/realms/binance/protocol/openid-connect/token` |
| Introspection | `https://AUTH_HOST/realms/binance/protocol/openid-connect/token/introspect` |
| MCP resource | `https://MCP_RESOURCE_HOST/mcp` |
| Protected-resource metadata | `https://MCP_RESOURCE_HOST/.well-known/oauth-protected-resource/mcp` |

`AUTH_HOST` and `MCP_RESOURCE_HOST` remain user-supplied values. The code does
not implement Keycloak or any other provider; it only validates the provider's
introspection response. The provider must support Authorization Code with PKCE
`S256`, advertise refresh/offline access, and issue tokens whose issuer and
audience/resource exactly match the configured values.

In ChatGPT Advanced OAuth, use the issuer/discovery metadata, authorization
URL, and token URL above (or the values returned by discovery). Register the
exact redirect URI displayed by ChatGPT; never guess it. Use the provider's
supported client-registration method: prefer CIMD, otherwise use DCR or a
predefined client when the ChatGPT form/provider requires it. ChatGPT does not
use client-credentials OAuth for this connection. The introspection URL and
resource-server client secret belong only in the scanner's protected
environment, never in ChatGPT.

The tunnel does not automatically expose a private authorization server. The
authorization server's discovery and authorization endpoints must be reachable
by the OAuth browser flow, even when MCP requests themselves travel through
Secure MCP Tunnel.

After explicit provider/hosting authorization: enter the exact HTTPS URL
ending in `/mcp` in ChatGPT developer mode, select OAuth, complete the
provider’s PKCE flow, grant `binance:read`, and grant `binance:draft` only when
draft creation is intended. Then verify tool discovery with `get_bot_status`
and confirm a test draft is still `unapproved`.

Troubleshooting: authentication loops usually indicate a wrong endpoint,
issuer, callback registration, PKCE, refresh access, or consent scope;
discovery failures indicate incorrect protected-resource metadata or resource
URL; tool-listing failures indicate a lifespan/migration/Streamable HTTP issue;
read-success/draft-failure means the token lacks `binance:draft`. Never disable
authentication to resolve these failures.

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
