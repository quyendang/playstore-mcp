# Play Store MCP Bridge

**An authenticated remote MCP server for the Google Play Developer API.** Self-host on Railway, then connect Claude, Codex, ChatGPT, Cursor, Gemini, or another compatible MCP client to `https://your-domain/mcp` using Google OAuth.

The bridge preserves **all 117 original Google Play tools and their input schemas**, and adds **6 tools** for remote files and persistent background operations. Python/FastMCP calls the API directly; the remote service does not wrap a stdio subprocess. A separate local stdio entrypoint remains available.

## Contents

- [1. Authentication and access model](#authentication)
- [2. Install from source](#installation)
- [3. Set up the Google Play service account](#service-account)
- [4. Set up Google OAuth for MCP sign-in](#oauth)
- [5. Deploy on Railway](#railway)
- [6. Connect MCP clients after deployment](#clients)
- [7. Verify your setup and troubleshoot](#troubleshooting)
- [8. Upload and download files](#files)
- [9. Run background operations](#operations)
- [10. Endpoints and configuration](#configuration)
- [11. Development and attribution](#development)

<a id="authentication"></a>

## 1. Authentication and access model

This project uses two separate Google authentication mechanisms:

| Component | Purpose | Configuration |
|---|---|---|
| **Google OAuth Web client** | Identifies people signing in to MCP and the file portal | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` |
| **Google service account** | Authorizes calls to the Google Play Developer API | `GOOGLE_PLAY_STORE_CREDENTIALS` and Play Console permissions |
| **Bridge allowlist** | Restricts which Google identities may use the bridge | `BRIDGE_ALLOWED_EMAILS` or `BRIDGE_ALLOWED_SUBJECTS` |

Signing in with Google does not grant the service account access to your apps. A valid service-account key also does not replace MCP OAuth sign-in. Configure both before connecting a client.

**Allowed users share the configured Play service account and file library.** Background-operation results are private to the submitting Google subject. This model suits an individual or a trusted team. For independent Play accounts, deploy separate instances unless you deliberately use the advanced per-request credential path described below.

For a normal remote connection, users need the server URL and their Google sign-in. They do not need to paste a service-account key into Claude or Codex.

<a id="installation"></a>

## 2. Install from source

You need Git, Python 3.11+, and [uv](https://docs.astral.sh/uv/getting-started/installation/). The remote bridge also requires Redis, writable persistent storage, and a public HTTPS origin.

```bash
git clone https://github.com/quyendang/playstore-mcp.git
cd playstore-mcp
uv sync --frozen
```

There are two entrypoints:

| Command | Behavior |
|---|---|
| `uv run --frozen playstore-mcp-bridge` | Starts the authenticated remote HTTP bridge, including the portal and job queue |
| `uv run --frozen play-store-mcp` | Starts the original local stdio server |

**The Dockerfile starts the remote bridge by default.** Running `uvx play-store-mcp` installs the upstream PyPI package, which is not the same as running this bridge from source.

For local remote-bridge development, run Redis locally and use an HTTPS tunnel. Set `BRIDGE_PUBLIC_URL` to the tunnel origin, register both Google callbacks in section 4, and choose a writable `BRIDGE_DATA_DIR`. Export the required variables before running `playstore-mcp-bridge`; the application does not automatically load `.env` files.

The shell examples below use Bash/Zsh on macOS or Linux. Replace domains, paths, email addresses, and package names with your own values.

<a id="service-account"></a>

## 3. Set up the Google Play service account

### 3.1. Enable the API

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create or select a project.
2. Go to **APIs & Services → Library**.
3. Find **Google Play Android Developer API** (`androidpublisher.googleapis.com`) and enable it in the project containing your service account.

Google no longer requires linking your Play developer account to a Cloud project through the old API access page. See [Google Play API setup](https://developers.google.com/android-publisher/getting_started).

### 3.2. Create the service account and JSON key

1. Open **IAM & Admin → Service Accounts → Create service account**.
2. Give it a name such as `playstore-mcp` and finish creating it. App access is granted separately in Play Console; Cloud Project Owner is not required for that purpose.
3. Open the account, then select **Keys → Add key → Create new key → JSON → Create**.
4. Save the downloaded file outside your repository. Note its `client_email` for the next step.

Keep the original `private_key`, `client_email`, and `token_uri` values. If key creation is blocked by an organization policy, ask your administrator to review that policy. See [Google's service-account key instructions](https://cloud.google.com/iam/docs/keys-create-delete).

Do not commit the key. On macOS/Linux, restrict access to the local file:

```bash
chmod 600 /absolute/path/service-account.json
```

### 3.3. Grant access in Google Play Console

1. Open [Play Console](https://play.google.com/console/) → **Users and permissions → Invite new users**.
2. Enter the service account's **`client_email`**, not the personal email used to sign in to MCP.
3. Under **App permissions**, select the relevant apps, grant the permissions you need, and invite the user.

| Task | Play Console permission to consider |
|---|---|
| Read app information | View app information (read-only) |
| Publish to testing tracks | Release apps to testing tracks |
| Publish production releases | Release to production, exclude devices, and use Play App Signing |
| Edit tester lists | Manage testing tracks and edit tester lists |
| Edit listings and products | Manage store presence |
| Reply to reviews | Reply to reviews |
| Billing, orders, and subscriptions | View financial data, orders, and cancellation survey responses; Manage orders and subscriptions |

Grant permissions for the intended tasks and app scope. See the [Play Console permission reference](https://support.google.com/googleplay/android-developer/answer/9844686); Google's [API setup guide](https://developers.google.com/android-publisher/getting_started) lists the two Billing permissions in the final row.

A downloaded JSON key alone is insufficient. Cloud IAM roles do not replace Play Console app permissions.

### 3.4. Configure the key on the server

On Railway, create a **sealed variable** named `GOOGLE_PLAY_STORE_CREDENTIALS` and paste the complete JSON file contents. Do not wrap the JSON in shell quotes in the dashboard.

For a local process:

```bash
export GOOGLE_PLAY_STORE_CREDENTIALS="$(cat /absolute/path/service-account.json)"
```

Alternatively, set `GOOGLE_APPLICATION_CREDENTIALS` to an absolute JSON file path on the server. In Docker, that path must exist inside the container. Prefer one method; inline `GOOGLE_PLAY_STORE_CREDENTIALS` takes precedence over the file path.

<a id="oauth"></a>

## 4. Set up Google OAuth for MCP sign-in

### 4.1. Choose your public origin

For example:

```text
https://playstore-mcp.example.com
```

Set `BRIDGE_PUBLIC_URL` to this HTTPS origin, **without `/mcp`, another path, a query, or a fragment**. When changing domains, update this value and both Google redirect URIs together.

### 4.2. Configure the consent screen and audience

In Google Cloud, open **Google Auth Platform**; older interfaces may call this **OAuth consent screen**. Configure the app name, support email, contact information, and appropriate audience. If the app is in Testing, add the people who will sign in as test users. See [Google OAuth app branding](https://support.google.com/cloud/answer/15549049).

The bridge allowlist is a separate check: those users must also appear in `BRIDGE_ALLOWED_EMAILS` or match an allowed Google subject. Adding a bridge user does not automatically add a Google test user. Review Google's publishing and verification requirements before relying on a Testing configuration for long-term use.

### 4.3. Create a Web application OAuth client

1. Open **Google Auth Platform → Clients → Create client**.
2. Choose **Web application**.
3. Add these exact **Authorized redirect URIs**, using your own domain:

```text
https://playstore-mcp.example.com/auth/callback
https://playstore-mcp.example.com/portal/callback
```

4. Create the client and save its ID and secret in your hosting secrets:

```text
GOOGLE_OAUTH_CLIENT_ID=YOUR_WEB_CLIENT_ID.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=YOUR_WEB_CLIENT_SECRET
```

`/auth/callback` handles MCP authorization; `/portal/callback` handles browser sign-in to the file portal. Do not use `/mcp` as the Google redirect URI. See [Google Web OAuth](https://developers.google.com/identity/protocols/oauth2/web-server) and [OAuth client management](https://support.google.com/cloud/answer/15549257).

The bridge requests `openid email profile` to identify users. Google Play API calls use the separate service account. Keep the Google OAuth client secret on the server.

<a id="railway"></a>

## 5. Deploy on Railway

### 5.1. Create the service and storage

1. Create a Railway service from **`quyendang/playstore-mcp`**, or your own fork. The repository includes a Dockerfile and `railway.json`.
2. Add **Redis** in the same project/environment. Keep persistence enabled so OAuth registrations and refresh tokens survive restarts.
3. Attach a **Volume** to the app at `/data`. Use **one app replica**. The entrypoint initializes volume ownership, then drops privileges to UID 10001.
4. Generate a Railway domain or attach a custom domain. Use it for `BRIDGE_PUBLIC_URL` and both Google callbacks.

An initial deployment may be unhealthy until all required settings are present. Finish configuring the variables, then redeploy. See [Railway volumes](https://docs.railway.com/volumes).

### 5.2. Set variables on the app service

| Variable | Value |
|---|---|
| `BRIDGE_PUBLIC_URL` | `https://playstore-mcp.example.com`, without `/mcp` |
| `GOOGLE_OAUTH_CLIENT_ID` | Google OAuth **Web application** client ID |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google OAuth Web client secret |
| `BRIDGE_SIGNING_KEY` | A stable random secret of at least 32 characters |
| `BRIDGE_ALLOWED_EMAILS` | Comma-separated Google email addresses allowed to use the bridge |
| `BRIDGE_ADMIN_EMAILS` | Admin emails; each must also be in `BRIDGE_ALLOWED_EMAILS` |
| `REDIS_URL` | `${{Redis.REDIS_URL}}`; replace `Redis` with your actual service name |
| `GOOGLE_PLAY_STORE_CREDENTIALS` | Complete service-account JSON in a sealed variable |
| `BRIDGE_DATA_DIR` | `/data` |
| `PLAY_STORE_MCP_READ_ONLY` | Start with `1`; use `0` when you intend to enable Google Play writes |

Generate a signing key locally:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Keep it stable and back it up securely. Changing it invalidates sessions/tokens and prevents reading existing job records encrypted with the old key.

For example, set `BRIDGE_ALLOWED_EMAILS` to `you@example.com,teammate@example.com`. These are human Google identities, not the service account's `client_email`. Every allowed user receives access to the shared Play account and file library.

### 5.3. Deploy and check the endpoints

Apply the settings and redeploy. The bridge listens on `0.0.0.0`, using Railway's `PORT`, then `MCP_PORT`, then port 8000. The health check in `railway.json` targets **`/ready`**.

```bash
curl --fail https://playstore-mcp.example.com/health
curl --fail https://playstore-mcp.example.com/ready
curl -i https://playstore-mcp.example.com/mcp
```

- `/health` checks the process and returns `service: playstore-mcp-bridge` and `original_tools: 117`.
- `/ready` checks Redis, SQLite, and whether a Play client has initialized; success returns `{"status":"ready"}`. It does not verify permissions on every app.
- An unauthenticated `/mcp` request should require OAuth, normally with HTTP 401. Use an MCP client to complete the connection.

Open `/portal` and sign in with an allowed email to verify the portal callback. The bridge already protects its remote tools with OAuth; a manually configured static Bearer token is not required for normal client setup.

<a id="clients"></a>

## 6. Connect MCP clients after deployment

Replace the example domain with your deployed domain. Use **`https://playstore-mcp.example.com/mcp`**, sign in to Google, and approve the bridge connection. Your email must pass both Google's app access rules and the bridge allowlist.

### Claude Code

```bash
claude mcp add --scope user --transport http playstore \
  https://playstore-mcp.example.com/mcp
claude mcp list
```

Open Claude Code, run `/mcp`, select the server, and complete authentication in your browser. CLI versions that support it can also run `claude mcp login playstore`. No custom credential header is needed. See [Claude Code MCP](https://code.claude.com/docs/en/mcp).

### Codex CLI / Codex app

```bash
codex mcp add playstore --url https://playstore-mcp.example.com/mcp
codex mcp login playstore
codex mcp list
```

Alternatively, add this to `~/.codex/config.toml`, then run the login command:

```toml
[mcp_servers.playstore]
url = "https://playstore-mcp.example.com/mcp"
```

Complete Google sign-in and approve the bridge connection. Start a new chat after configuration changes; use `/mcp` in the CLI to inspect the connection. In the desktop app, configure the remote MCP server and choose Authenticate when prompted. See [OpenAI's MCP documentation](https://developers.openai.com/codex/mcp/).

### Claude Desktop

If your version offers custom remote connectors, add the `/mcp` URL in its connector settings and complete OAuth. Menu names and availability depend on your app version and account.

For a stdio configuration, [mcp-remote](https://github.com/punkpeye/mcp-remote) can connect to the remote OAuth server. Install Node.js LTS with `npx`, then merge this entry into your existing configuration:

```json
{
  "mcpServers": {
    "playstore": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://playstore-mcp.example.com/mcp"]
    }
  }
}
```

The `claude_desktop_config.json` locations are documented in the [MCP Desktop guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers):

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Quit and reopen Claude, then complete browser sign-in. If the app cannot find `npx`, use its absolute executable path.

### Cursor

Add this entry to `~/.cursor/mcp.json` or your project's MCP configuration:

```json
{
  "mcpServers": {
    "playstore": {
      "url": "https://playstore-mcp.example.com/mcp"
    }
  }
}
```

Enable the server in MCP settings, complete OAuth when prompted, and make its tools available to the Agent. See [Cursor MCP](https://cursor.com/docs/context/mcp).

### Gemini CLI

Merge this into `mcpServers` in `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "playstore": {
      "httpUrl": "https://playstore-mcp.example.com/mcp"
    }
  }
}
```

Gemini CLI uses `httpUrl` for Streamable HTTP. Restart the CLI, inspect `/mcp`, and complete the server's OAuth flow. See [Gemini CLI MCP](https://geminicli.com/docs/tools/mcp-server/).

### ChatGPT and Gemini web

- **ChatGPT:** add a custom MCP server/app/plugin with the `/mcp` URL, choose OAuth, and complete Google sign-in. Use a connection with the appropriate tool capabilities for write actions. Labels and availability depend on your account; see [OpenAI custom MCP](https://developers.openai.com/api/docs/guides/custom-mcp-server).
- **Gemini web:** under **Connected Apps → Custom apps**, enter the same URL and authorize it. Server compatibility cannot enable an account feature that is unavailable to you. See [Google's custom-app instructions](https://support.google.com/gemini/answer/17209137?hl=en-12).

The Google OAuth client ID and secret belong **on the server**. Do not paste them into the client examples above. Client-to-bridge callbacks are handled by MCP OAuth; the two Google redirect URIs in section 4 remain the bridge callbacks.

<a id="troubleshooting"></a>

## 7. Verify your setup and troubleshoot

After the client reports a successful connection:

1. Ask: **“Use Play Store MCP to validate the package name `com.yourcompany.yourapp`.”** `validate_package_name` verifies tool access, without calling Google Play.
2. Ask: **“Use `get_releases` to read releases for `com.yourcompany.yourapp`. Do not change anything.”** Replace this with a real app accessible to your service account.
3. Open `/portal` and test file upload if you need remote deployment tools.

`/mcp` exposes **117 original tools plus 6 bridge tools**. It does not collapse them into three meta-tools. Connect to `/mcp-code` separately if you want code mode.

| Symptom | Check |
|---|---|
| `BRIDGE_PUBLIC_URL must be an HTTPS origin` | Use `https://domain`, without `/mcp`, another path, or a query |
| `redirect_uri_mismatch` | Match scheme, hostname, and both Google callbacks exactly; verify the Web client ID |
| Google denies access while the app is in Testing | Check the OAuth audience and Google test-user list |
| Account is not on the allowlist | Add the verified human Google email to `BRIDGE_ALLOWED_EMAILS` |
| Admin email must also be allowed | Include every admin email in `BRIDGE_ALLOWED_EMAILS` |
| `/health` succeeds but `/ready` returns 503 | Check Redis, the SQLite volume, and Play credential initialization in deployment logs |
| `/ready` succeeds but Play API returns 403 | Verify service-account `client_email`, app scope, and task permissions in Play Console |
| App not found / Google returns 404 | Use the actual application ID and grant access to that app |
| `/mcp` returns 401 before sign-in | Complete OAuth through the MCP client; a service-account JSON key is not a bridge Bearer token |
| Origin-related HTTP 403 | Check the Origin sent by the client/proxy against `BRIDGE_PUBLIC_URL` |
| `Invalid host header` | Match the public domain/Host to the hostname in `BRIDGE_PUBLIC_URL` |
| Laptop file path is not found | Upload through `/portal` and use `asset://ID`; chat attachments are not transferred automatically |
| Google Play writes are blocked | Set `PLAY_STORE_MCP_READ_ONLY=0`, redeploy, and verify Play Console permissions |
| Tokens/job records fail after a deployment | Check for signing-key changes, Redis persistence, and the `/data` volume |
| An operation has status `unknown` | Check Google Play before submitting a new operation |

Exclude service-account JSON, OAuth secrets, signing keys, and token-bearing download links when sharing logs.

<a id="files"></a>

## 8. Upload and download files

Files attached to a conversation are **not automatically uploaded** to this server. Open `https://your-domain/portal`, sign in, upload an APK/AAB/image, and copy its `asset://ID` reference.

The portal uploads in chunks. To resume, enter the upload ID and select the same original file. Compare SHA-256 when verifying a resumed artifact.

The original `file_path`, `apk_path`, `bundle_path`, and `image_path` parameters accept `asset://ID`. Generated/system APK downloads retain `destination_path` as the output filename and add `asset_id` and a temporary `download_url`. Use `get_asset` to obtain a fresh link after expiry. Responses stream bytes and support HTTP range downloads; transfer size and storage quotas are enforced.

Remote file paths are confined to the asset directory. The local stdio entrypoint retains its original file behavior.

For CI/API uploads, authenticate with a **bridge OAuth Bearer token**:

1. `POST /api/assets` with `{"name":"app.aab","size":12345}`.
2. `PUT /api/assets/ID/chunks?offset=0` with raw chunk bytes, within the configured chunk limit. Continue at the offset returned by the server.
3. `POST /api/assets/ID/complete` with `{}` or `{"sha256":"expected-hash"}`.
4. Use the returned asset reference in an original tool.

Do not put a service-account key in a URL. Browser mutations using a session cookie require a same-origin request. Download links carry temporary authorization and should be kept private.

<a id="operations"></a>

## 9. Run background operations

`start_operation(tool_name, arguments, idempotency_key)` queues any original tool for durable execution using the **shared Play account configured on the server**. Use it for large artifacts, long deployments, downloads, or batches so a client request timeout does not cancel the operation.

Example arguments for `start_operation`:

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

Poll `get_operation(operation_id)`, or call `list_operations()` after reconnecting. The original tool result appears under `result`. The same owner/key/arguments return the same job; reusing a key with different arguments is rejected. Queue payloads and results are encrypted on the Volume.

Statuses are `queued`, `running`, `completed`, `failed`, and `unknown`. A restart during execution or an unsuccessful write result is conservatively marked `unknown`: check Google Play before creating a new job. In-flight writes are not automatically replayed; queued jobs resume after restart. Google edits for the same package are serialized across direct calls and jobs.

Use jobs where duplicate write submissions matter. Direct tools retain their original semantics. Jobs reject per-request credential headers so a caller's service-account key is not persisted in the queue.

<a id="configuration"></a>

## 10. Endpoints and configuration

| Endpoint | Purpose |
|---|---|
| `/mcp` | OAuth-protected Streamable HTTP with all 123 tools |
| `/sse`, `/messages/` | Legacy HTTP+SSE compatibility with the same tools |
| `/mcp-code` | Optional `search`, `get_schema`, `execute` code-mode surface |
| `/portal` | Google-authenticated shared file management |
| `/api/assets` | Authenticated file management/upload API |
| `/credentials` | Admin-only, in-memory update to shared Play credentials |
| `/health`, `/ready` | Process and readiness probes |

All transport aliases share one OAuth resource: the public origin. OAuth registrations and tokens are encrypted in Redis. The allowlist is checked on authenticated requests.

### Write permissions and code mode

- `PLAY_STORE_MCP_READ_ONLY=1` blocks all **73 Google Play write tools**. Local file uploads, downloads, and deletion remain available.
- To enable writes, grant the appropriate Play Console permissions, set the variable to `0`, and redeploy.
- `/mcp-code` can execute multiple tools under one client approval. Prefer `/mcp` when you want the client to present individual tools.
- `BRIDGE_CODE_MODE=0` disables `/mcp-code`. The upstream `CODE_MODE` variable controls the separate stdio surface.

### Optional limits

See [.env.example](.env.example) for the complete example configuration.

| Variable | Default / purpose |
|---|---|
| `BRIDGE_ALLOWED_SUBJECTS` | Optional comma-separated Google subject IDs allowed to connect |
| `BRIDGE_MAX_FILE_BYTES` | 8 GiB per file |
| `BRIDGE_MAX_CHUNK_BYTES` | 8 MiB per chunk |
| `BRIDGE_MAX_STORAGE_BYTES` | 32 GiB reserved file storage; adjust to your actual Volume capacity |
| `BRIDGE_MAX_PENDING_JOBS` | 100 |
| `BRIDGE_DOWNLOAD_TTL` | 900 seconds |
| `PLAY_STORE_MCP_LOG_LEVEL` | `INFO` |

### Per-request credentials and key rotation

Direct tools still support `X-Google-Credentials` / `X-Google-Credentials-Base64` **after bridge OAuth authorization**. This advanced path is not needed for normal client connections. Base64 contains the complete private key; send it only to a trusted server over HTTPS. Background operations always use the shared credentials and reject these headers.

`POST /credentials` accepts the original JSON/Base64 payload formats, but requires an identity in `BRIDGE_ADMIN_EMAILS`. It updates shared credentials in memory only; update the Railway secret to persist a change across restarts. The upstream `PLAY_STORE_MCP_ADMIN_TOKEN` does not replace bridge OAuth admin access.

### Local stdio compatibility

After exporting your Google Play credentials:

```bash
CODE_MODE=0 uv run --frozen play-store-mcp --read-only
```

A stdio client starts this command itself; Redis and bridge OAuth are not required. Use `uv --directory /absolute/path/playstore-mcp run --frozen play-store-mcp --read-only` when the client's working directory differs. See [UPSTREAM_README.md](UPSTREAM_README.md) for upstream/local behavior.

<a id="development"></a>

## 11. Development and attribution

| Original tool group | Count |
|---|---:|
| Publishing and batch deploy | 8 |
| Listings and images | 7 |
| Reviews and testers | 5 |
| Products, options, subscription catalogs/plans/offers | 47 |
| Purchases, orders, and external transactions | 16 |
| Users/grants, data safety, device tiers, app recovery | 16 |
| APK/AAB, expansion/deobfuscation, generated/system APK, internal sharing | 15 |
| Validation | 3 |
| **Original total** | **117** |

The six bridge tools are `start_operation`, `get_operation`, `list_operations`, `list_assets`, `get_asset`, and `delete_asset`. The test suite checks the [tool manifest](src/playstore_mcp_bridge/tool_manifest.json) against the original implementation, including input schemas and write guards.

```bash
uv sync --frozen --extra dev
uv run --frozen pytest -q -o addopts='' --ignore=tests/test_integration.py --ignore=tests/test_live_api.py
uv run --frozen ruff check src/ tests/ docker_entrypoint.py
uv run --frozen ruff format --check src/ tests/ docker_entrypoint.py
uv run --frozen mypy src/
uv build
```

Live Google tests are opt-in. Bridge tests use mocked Google calls and in-memory Redis. This README describes the authenticated remote bridge; `docs/` and `UPSTREAM_README.md` retain upstream/local documentation.

Based on [lusky3/play-store-mcp](https://github.com/lusky3/play-store-mcp), source snapshot `aebcd31`, under the original [MIT License](LICENSE). The original Google API implementation and tests are retained. See also [FastMCP OAuth](https://gofastmcp.com/servers/auth/oauth-proxy).
