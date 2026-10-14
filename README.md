# Play Store MCP Bridge

Authenticated Google Play MCP server for ChatGPT, Gemini and other remote clients. Deploy on Railway and connect with `https://your-domain/mcp`.

All **117 original Google Play tools** are preserved, with matching input schemas. Six additional tools provide remote files and persistent background operations. Python/FastMCP runs the API directly; there is no stdio subprocess bridge.

## Railway deployment

1. Deploy this GitHub repository as a Railway service. Its Dockerfile and `railway.json` are detected automatically.
2. Add a **Redis** service. Keep Redis persistence enabled so OAuth registrations and refresh tokens survive restarts.
3. Attach a **Volume** to the app at `/data`. Use one app replica. The container initializes the volume and then drops root privileges.
4. Generate a Railway domain or attach your custom domain.
5. Create a Google OAuth **Web application** client, with these two authorized redirect URIs:
   - `https://your-domain/auth/callback` — MCP authorization
   - `https://your-domain/portal/callback` — file portal
6. For a Google OAuth app in Testing, add every allowed user as a test user. Google may expire refresh grants in Testing; use the appropriate production publishing configuration for lasting access.
7. Set these Railway variables before the first healthy deployment:

| Variable | Value |
|---|---|
| `BRIDGE_PUBLIC_URL` | `https://your-domain`, **without `/mcp`** |
| `GOOGLE_OAUTH_CLIENT_ID` | Google OAuth web client ID |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google OAuth web client secret |
| `BRIDGE_SIGNING_KEY` | A stable, randomly generated secret of at least 32 characters |
| `BRIDGE_ALLOWED_EMAILS` | Comma-separated verified Google emails allowed to use all tools |
| `BRIDGE_ADMIN_EMAILS` | Allowed emails that may update the shared Play credentials |
| `REDIS_URL` | `${{Redis.REDIS_URL}}`, adjusted to your Redis service name |
| `GOOGLE_PLAY_STORE_CREDENTIALS` | The complete Google Play service-account JSON in a sealed variable |
| `BRIDGE_DATA_DIR` | `/data` |

Generate a signing key locally: `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`. Keep it stable and back it up securely; changing it invalidates OAuth/portal tokens and prevents reading existing encrypted job records.

The service binds to `0.0.0.0:$PORT`. `/health` checks the process; `/ready` checks Redis, SQLite and that a Play API client can initialize. Readiness does **not** verify permissions on every app. Google Play permissions are checked when a tool calls the API. Logs omit HTTP access URLs because download links carry short-lived tokens.

Optional settings, quotas and local configuration are in [.env.example](.env.example). File and queue limits can be increased to match your volume capacity. Set `PLAY_STORE_MCP_READ_ONLY=1` to block all 73 Google Play write tools. Local file upload/download/delete remain available.

## Connect clients

- **ChatGPT:** add a custom MCP server/plugin with `https://your-domain/mcp`, select OAuth, complete Google login and approve the bridge connection. Use the full tool connection for write actions. UI labels/availability depend on your ChatGPT account.
- **Gemini web:** Connected Apps → Custom apps → enter the same URL and authorize. Google's current consumer availability restrictions apply; a compatible server cannot enable a missing account feature. Gemini Enterprise Business uses Streamable HTTP as well.
- **Other MCP clients:** use the same OAuth-protected Streamable HTTP endpoint.

Endpoints:

| Endpoint | Purpose |
|---|---|
| `/mcp` | Streamable HTTP; all 117 original tools plus 6 bridge tools |
| `/sse` and `/messages/` | Legacy HTTP+SSE compatibility, same tool set |
| `/mcp-code` | Optional `search`, `get_schema`, `execute` code-mode surface |
| `/portal` | Google-authenticated upload/file management |
| `/credentials` | Admin-only, in-memory shared credential update |
| `/health`, `/ready` | Railway process/readiness probes |

All transport aliases share one OAuth resource, the public origin. OAuth discovery advertises that common parent so tokens have a stable audience even when `/mcp-code` is enabled. OAuth registrations and tokens are encrypted in Redis. An allowed Google identity is checked on each authenticated request. Anyone with an allowed account has access to the **same Play service account and shared file library**. Job results are private to their submitting Google subject.

Code mode can execute multiple write tools under one client approval. Prefer `/mcp` for action-by-action confirmation; code mode remains available for compatible clients. Disable only this optional surface with `BRIDGE_CODE_MODE=0`.

## Upload and download files

Files attached to a ChatGPT/Gemini conversation are not automatically transferred to this server. Open `/portal`, log in, upload an artifact, then copy its `asset://ID` reference. The portal uploads in chunks and lets you resume by entering the upload ID and choosing the same file again. Compare SHA-256 when verifying a resumed artifact.

The original `file_path`, `apk_path`, `bundle_path` and `image_path` parameters accept `asset://ID`. Original return fields are retained. Remote server paths are confined to the asset directory; the local stdio command keeps its original file behavior.

Generated/system APK download tools retain `destination_path` as the output filename and add `asset_id` plus a temporary `download_url`. Use `get_asset` for a fresh link after expiry. Artifact responses stream bytes and support HTTP range downloads. Download size and volume quotas are enforced during media transfer.

Authenticated CI/API upload:

1. `POST /api/assets` with `{"name":"app.aab","size":12345}`.
2. `PUT /api/assets/ID/chunks?offset=0` with raw file bytes (up to the configured chunk limit). Repeat at the returned offset.
3. `POST /api/assets/ID/complete` with `{}` or `{"sha256":"expected-hash"}`.
4. Use the returned reference in an original MCP tool.

Use an OAuth Bearer token for API calls. Never put the Google service-account key in a URL. Browser mutations additionally require a same-origin request.

## Background operations

`start_operation(tool_name, arguments, idempotency_key)` invokes **any original tool** through a persistent queue, using the configured shared Play account. Use it for large artifacts or long batches so a client request timeout does not cancel the operation.

Example arguments:

```json
{
  "tool_name": "deploy_app",
  "arguments": {
    "package_name": "com.example.app",
    "track": "internal",
    "file_path": "asset://YOUR_ASSET_ID"
  },
  "idempotency_key": "com.example.app-internal-build-42"
}
```

Then call `get_operation(operation_id)` until the status is terminal, or `list_operations()` after reconnecting. The original tool result is returned under `result`. The same owner/key/arguments return the same job. Reusing a key with different arguments is rejected. Queue payloads and results are encrypted on the Volume.

Statuses are `queued`, `running`, `completed`, `failed`, `unknown`. A restart during execution or an unsuccessful write result is conservatively marked `unknown`: check Google Play before creating another job. No in-flight write is automatically replayed. Queued jobs resume after restart. Google edits for a package are serialized across direct calls and jobs. Use the job path for writes where duplicate submission would matter; direct tools retain their original semantics.

## Original functionality

| Group | Tools |
|---|---:|
| Publishing and batch deploy | 8 |
| Listings and images | 7 |
| Reviews and testers | 5 |
| In-app/one-time products, options, subscription catalogs/plans/offers | 47 |
| Purchase status/management, orders, external transactions | 16 |
| Users/grants, data safety, device tiers, app recovery | 16 |
| APK/AAB, expansion/deobfuscation, generated/system APK, internal sharing | 15 |
| Validation | 3 |
| **Original total** | **117** |

Bridge tools: `start_operation`, `get_operation`, `list_operations`, `list_assets`, `get_asset`, `delete_asset`. The [manifest](src/playstore_mcp_bridge/tool_manifest.json) is checked against the original source, including every write guard, by the test suite.

Per-request `X-Google-Credentials` / `X-Google-Credentials-Base64` headers remain supported for direct tools, **after bridge OAuth authorization**. Background jobs deliberately use the shared configured account, so no caller's service-account key is persisted in the queue. `/credentials` accepts the original JSON/base64 payload formats, restricted to bridge admins; persist credential changes through Railway variables when required.

Local stdio compatibility: `uv run play-store-mcp` with the original Google credential environment variables. `CODE_MODE=0` exposes the original 117-tool list in stdio; its existing read-only CLI flag remains available. The Docker entrypoint starts the authenticated HTTP bridge by default.

## Development and validation

```bash
uv sync --frozen --extra dev
uv run --frozen pytest -q -o addopts='' --ignore=tests/test_integration.py --ignore=tests/test_live_api.py
uv run --frozen ruff check src/ tests/ docker_entrypoint.py
uv run --frozen ruff format --check src/ tests/ docker_entrypoint.py
uv run --frozen mypy src/
uv build
```

Live Google tests are opt-in; tests for the new bridge use mocked Google calls and in-memory Redis. For local bridge execution, provide the environment settings, a local Redis instance and a writable `BRIDGE_DATA_DIR`, then run `uv run playstore-mcp-bridge`. Use an HTTPS tunnel whose origin matches `BRIDGE_PUBLIC_URL` and both Google callback URIs.

## Sources and attribution

Based on [lusky3/play-store-mcp](https://github.com/lusky3/play-store-mcp), source snapshot `aebcd31`, under its original MIT [license](LICENSE). The original Google API implementation and tests are retained. [UPSTREAM_README.md](UPSTREAM_README.md) and `docs/` describe upstream/local behavior; this README governs the authenticated Railway bridge.

Protocol/client documentation: [OpenAI custom MCP](https://developers.openai.com/api/docs/guides/custom-mcp-server), [Gemini custom apps](https://support.google.com/gemini/answer/17209137?hl=en-12), [FastMCP OAuth](https://gofastmcp.com/servers/auth/oauth-proxy), [Railway volumes](https://docs.railway.com/volumes).
