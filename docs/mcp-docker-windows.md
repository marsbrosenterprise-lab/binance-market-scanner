# Docker on Windows connection preparation

This guide prepares the existing scanner for a later authenticated ChatGPT
connection. It does not create an account, tunnel, DNS record, OAuth client,
or public endpoint.

## Recommended design

Use this two-stage design:

1. Run the existing API and PostgreSQL containers locally. Keep the API bound
   to `127.0.0.1:8000`; PostgreSQL is already bound to localhost only.
2. If available for your ChatGPT workspace, use OpenAI’s Secure MCP Tunnel as
   the transport from ChatGPT to the local MCP endpoint. Keep OAuth as the
   MCP authentication layer.
3. Use a portable Keycloak realm as the selected OAuth provider. It supports
   OIDC discovery, Authorization Code with PKCE, refresh/offline access, token
   revocation, and introspection. Configure an audience mapper so the token’s
   `aud` or resource claim is the exact public MCP URL.

The tunnel and OAuth have different jobs: the tunnel provides a private route
to the Docker service; OAuth authenticates ChatGPT and grants
`binance:read`/`binance:draft`. A tunnel token is not an MCP bearer token and
must not be used as a substitute for provider validation.

Fallback design: use an HTTPS reverse proxy on a future server (or an
authorized tunnel) in front of the API and Keycloak. It is more portable but
requires a hostname, TLS, and external reachability. The proxy must expose
only `/mcp`, `/.well-known/oauth-protected-resource/mcp`, and the selected
provider’s discovery/authorization/token/introspection paths. It must return
404 for `/dashboard`, `/api/v1/*`, `/docs`, and `/openapi.json` on the public
listener.

OpenAI currently documents that ChatGPT cannot connect directly to localhost,
that custom apps use a remote MCP endpoint, and that OAuth connections should
provide refresh/offline access. Full write-capable MCP app support is plan and
workspace dependent. Verify availability in the ChatGPT workspace before
choosing the tunnel path.

## Local Windows setup

Run these commands in PowerShell from the repository directory. The commands
use placeholders and do not require Binance credentials:

```powershell
Copy-Item .env.example .env
notepad .env
docker compose config
docker compose up -d postgres api
docker compose ps
Invoke-WebRequest http://127.0.0.1:8000/health/live
Invoke-WebRequest http://127.0.0.1:8000/health/ready
```

Set a random local-only `MCP_AUTH_TOKEN` in `.env` before starting `api`, keep
`MCP_AUTH_MODE=static`, and leave these disabled:

```dotenv
TRADING_ENABLED=false
LIVE_CONVERT_ENABLED=false
LIVE_CONVERT_AUTO_EXECUTION_ENABLED=false
BINANCE_API_KEY=
BINANCE_API_SECRET=
LIVE_CONVERT_API_KEY=
LIVE_CONVERT_API_SECRET=
```

The local MCP URL is `http://127.0.0.1:8000/mcp`. Use `/mcp` exactly, not
`/mcp/`. The local protected-resource metadata URL is
`http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp`.

For shutdown, preserve the database volume and stop services without removing
records:

```powershell
docker compose stop api postgres
# Later:
docker compose start postgres api
```

Do not use `docker compose down -v` for this application; it removes the
PostgreSQL volume and trading/audit records.

## Selected provider: Keycloak

Keycloak is selected for the portable self-hosted option; no Keycloak
container is enabled by default and no external setup has been performed.
Create a realm and a public OIDC client only after an HTTPS/tunnel choice is
authorized. Configure:

- Authorization Code flow with PKCE method `S256`.
- The exact ChatGPT callback URI shown by the ChatGPT app-creation dialog.
- Requested scopes `openid offline_access binance:read`; add
  `binance:draft` only for deliberate draft-write testing.
- A protocol mapper or audience configuration that makes the access-token
  `aud`/resource equal the exact public MCP URL, including `/mcp`.
- An introspection client with permission to call the realm introspection
  endpoint, using a protected client secret.
- Token responses/introspection claims: `active`, `iss`, `exp`, optional
  `nbf`, `scope`, and `aud` or `resource`.
- Refresh/offline access and a revocation policy.

Then set the production values in protected environment configuration, not in
Git:

```dotenv
MCP_AUTH_MODE=introspection
MCP_AUTH_ISSUER_URL=https://AUTH_HOST/realms/binance
MCP_RESOURCE_URL=https://MCP_HOST/mcp
MCP_INTROSPECTION_URL=https://AUTH_HOST/realms/binance/protocol/openid-connect/token/introspect
MCP_INTROSPECTION_CLIENT_ID=resource-server-introspection
MCP_INTROSPECTION_CLIENT_SECRET=REPLACE_LOCALLY
MCP_REQUIRED_SCOPE=binance:read
MCP_DRAFT_SCOPE=binance:draft
```

The endpoint pattern above is a template, not a value to use before the realm
and hostname exist. Confirm the actual discovery and introspection URLs from
the selected Keycloak realm metadata. Never paste client secrets into chat.

## ChatGPT connection steps after external authorization

1. Confirm the workspace has developer mode/custom MCP apps and write support.
2. Obtain the exact ChatGPT callback URI from the app-creation dialog.
3. Register that URI and the two scopes in Keycloak.
4. Choose Secure MCP Tunnel if available; otherwise obtain an authorized
   HTTPS hostname and TLS edge. Do not expose port 8000 directly.
5. Set `MCP_RESOURCE_URL` to the externally reachable URL ending in `/mcp`.
6. Start/restart the API and verify protected-resource metadata through the
   external route.
7. In ChatGPT, create a private custom app, enter the exact `/mcp` endpoint,
   choose OAuth, complete PKCE, and scan tools.
8. Test `get_bot_status` and another read tool first.
9. Only then grant `binance:draft` and test one draft. Confirm its state is
   `unapproved`; it cannot approve or execute anything.

## What is exposed

The intended external surface is the MCP Streamable HTTP endpoint, its
protected-resource metadata, and the provider’s OAuth discovery/authorization
paths. Dashboard pages, approval endpoints, Binance keys, PostgreSQL, and
internal worker/monitor services remain private. The MCP read tools expose
scanner status, market data, configured plans, execution history, and account
summary according to the configured credentials. The draft tool writes an
unapproved proposal only. No trade, withdrawal, Convert acceptance, safety
control, or credential operation is exposed.

## Future server migration

1. Stop workers/API gracefully and back up PostgreSQL using a tested restore
   procedure; do not copy a live volume by filesystem drag-and-drop.
2. Restore the database on the server and run `alembic upgrade head`.
3. Copy only protected environment values into the server secret store.
4. Change `MCP_RESOURCE_URL`, the Keycloak issuer/audience mapper, callback
   registration, and proxy routes to the new HTTPS host.
5. Keep the same realm/client scopes only after verifying issuer and audience
   claims against the new resource URL.
6. Reconnect ChatGPT, rescan tools, test read-only access, then test one
   unapproved draft. Keep live trading and automatic Convert execution off
   until separately reviewed.

## Troubleshooting

- `401` is expected without a bearer token. Inspect the
  `resource_metadata` URL in `WWW-Authenticate`.
- A discovery loop usually means the public resource URL, issuer, callback,
  PKCE method, or refresh/offline configuration is wrong.
- A read call succeeding while draft creation fails means the token lacks
  `binance:draft`; that is intentional.
- A `404` on `/mcp/` means the client used the wrong trailing-slash path.
- If the public listener shows the dashboard or `/api/v1`, stop it and fix the
  proxy allowlist before connecting ChatGPT.
