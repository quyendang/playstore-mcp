"""Bridge parity, authorization, files, durable jobs and HTTP contract tests."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakeredis.aioredis import FakeRedis
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.tools import Tool
from key_value.aio.stores.memory import MemoryStore
from starlette.testclient import TestClient

from play_store_mcp import server as core
from play_store_mcp.client import PlayStoreClient, PlayStoreClientError
from playstore_mcp_bridge.app import create_app
from playstore_mcp_bridge.assets import Assets
from playstore_mcp_bridge.config import Settings
from playstore_mcp_bridge.identity import AccessDenied, AllowlistedGoogleProvider, allowed_identity
from playstore_mcp_bridge.jobs import Jobs
from playstore_mcp_bridge.portal import signed
from playstore_mcp_bridge.runtime import MANIFEST, TOOLS, WRITES, Runtime
from playstore_mcp_bridge.storage import Store

OWNER = {"sub": "owner", "email": "owner@example.com", "email_verified": True}
HEADERS = {
    "Authorization": "Bearer owner",
    "Accept": "application/json,text/event-stream",
    "MCP-Protocol-Version": "2025-11-25",
}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        public_url="https://bridge.example.com",
        signing_key="test-signing-key-" * 4,
        oauth_client_id="google-client-id",
        oauth_client_secret="google-client-secret",
        redis_url="redis://localhost:6379",
        allowed_emails=frozenset({"owner@example.com", "reader@example.com"}),
        admin_emails=frozenset({"owner@example.com"}),
        data_dir=tmp_path,
        max_chunk_bytes=4,
        max_file_bytes=100,
        max_storage_bytes=200,
    )


@pytest.fixture(autouse=True)
def restore_core_globals():
    saved_resolver, saved_flag = core.get_client_from_context, core.READ_ONLY
    yield
    core.get_client_from_context = saved_resolver
    core.set_read_only(saved_flag)


@pytest.fixture
def assets(settings):
    return Assets(settings, Store(settings.data_dir, settings.signing_key))


@pytest.fixture
def runtime(settings, assets):
    return Runtime(settings, assets)


@pytest.fixture
def app(settings, monkeypatch):
    monkeypatch.setattr(PlayStoreClient, "_get_service", lambda _self: MagicMock())
    provider = StaticTokenVerifier(
        tokens={
            "owner": {"client_id": "owner", "scopes": [], **OWNER},
            "reader": {
                "client_id": "reader",
                "scopes": [],
                "sub": "reader",
                "email": "reader@example.com",
                "email_verified": True,
            },
            "stranger": {
                "client_id": "stranger",
                "scopes": [],
                "sub": "stranger",
                "email": "stranger@example.com",
                "email_verified": True,
            },
        }
    )
    return create_app(settings, auth_provider=provider, redis_client=FakeRedis())


def rpc(client, method, params=None, path="/mcp", headers=None):
    response = client.post(
        path,
        headers=headers or HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            **({"params": params} if params is not None else {}),
        },
    )
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        return json.loads(
            next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
        )
    return response.json()


def test_manifest_matches_every_original_tool_and_guard():
    tree = ast.parse(Path(core.__file__).read_text())
    found, writes = set(), set()
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
            for d in fn.decorator_list
        ):
            found.add(fn.name)
            if any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id == "_read_only_block"
                for c in ast.walk(fn)
            ):
                writes.add(fn.name)
    assert found == set(TOOLS) and len(found) == 117
    assert writes == WRITES and len(writes) == 73
    assert len(MANIFEST) == 117


async def test_exact_input_schema_parity_for_all_117_tools(app):
    tools = {tool.name: tool for tool in await app.state.mcp.list_tools()}
    assert len(tools) == 123
    for name, original in TOOLS.items():
        assert tools[name].parameters == Tool.from_function(original).parameters, name
    assert {tool.name for tool in await app.state.code_mcp.list_tools()} == {
        "search",
        "get_schema",
        "execute",
    }


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "other", "email": "owner@example.com", "email_verified": False},
        {"sub": "other", "email": "stranger@example.com", "email_verified": True},
        {"email": "owner@example.com", "email_verified": True},
    ],
)
def test_identity_fails_closed(settings, claims):
    with pytest.raises(AccessDenied):
        allowed_identity(claims, settings)


def test_identity_subject_or_verified_email(settings):
    assert allowed_identity(OWNER, settings) == "owner"
    assert (
        allowed_identity(
            {"sub": "google-sub"}, replace(settings, allowed_subjects=frozenset({"google-sub"}))
        )
        == "google-sub"
    )


async def test_google_provider_rechecks_allowlist(settings):
    provider = AllowlistedGoogleProvider(settings, client_storage=MemoryStore())
    denied = AccessToken(
        token="token",
        client_id="stranger",
        scopes=[],
        claims={"sub": "stranger", "email": "other@example.com", "email_verified": True},
    )
    allowed = AccessToken(token="token", client_id="owner", scopes=[], claims=OWNER)
    with patch.object(GoogleProvider, "verify_token", new=AsyncMock(side_effect=[denied, allowed])):
        assert await provider.verify_token("token") is None
        assert await provider.verify_token("token") is allowed


def test_oauth_metadata_resource_is_stable_across_aliases(settings, monkeypatch):
    monkeypatch.setattr(PlayStoreClient, "_get_service", lambda _self: MagicMock())
    app = create_app(settings, redis_client=FakeRedis(decode_responses=True))
    with TestClient(app, base_url=settings.public_url) as client:
        metadata = client.get("/.well-known/oauth-protected-resource").json()
        assert metadata["resource"] == settings.public_url + "/"
        assert metadata["authorization_servers"] == [settings.public_url + "/"]
        oauth = client.get("/.well-known/oauth-authorization-server").json()
        assert oauth["authorization_endpoint"] == settings.public_url + "/authorize"
        for path in ("/mcp", "/mcp-code", "/sse"):
            response = client.post(path, json={}) if path != "/sse" else client.get(path)
            assert response.status_code == 401
            assert settings.public_url in response.headers["www-authenticate"]
        registration = client.post(
            "/register",
            json={
                "client_name": "test-client",
                "redirect_uris": ["https://chatgpt.com/test/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile",
            },
        )
        assert registration.status_code == 201, registration.text
        assert registration.json()["client_id"]


def test_http_auth_origins_tools_and_code_mode(app, settings):
    with TestClient(app, base_url=settings.public_url) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready", headers={"Host": "healthcheck.railway.app"}).status_code == 200
        assert client.get("/ready").status_code == 200
        assert client.post("/mcp", json={}).status_code == 401
        assert client.get("/api/assets").status_code == 401
        assert (
            client.post(
                "/mcp", headers={**HEADERS, "Origin": "https://evil.example"}, json={}
            ).status_code
            == 403
        )
        assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400
        init = rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "bridge-test", "version": "1"},
            },
        )
        assert init["result"]["serverInfo"]["name"] == "Play Store Bridge"
        tools = rpc(client, "tools/list")["result"]["tools"]
        assert len(tools) == 123
        result = rpc(
            client,
            "tools/call",
            {"name": "validate_package_name", "arguments": {"package_name": "com.example.app"}},
        )
        assert result["result"]["structuredContent"]["valid"] is True
        code = rpc(
            client,
            "tools/call",
            {
                "name": "execute",
                "arguments": {
                    "code": "return await call_tool('validate_track', {'track':'production'})"
                },
            },
            path="/mcp-code",
        )
        assert '"valid":true' in json.dumps(code).replace(" ", "").replace('\\"', '"')
        denied = rpc(
            client,
            "tools/call",
            {"name": "get_releases", "arguments": {"package_name": "com.example.app"}},
            headers={**HEADERS, "Authorization": "Bearer stranger"},
        )
        assert denied["result"]["isError"] is True
        assert (
            client.get("/api/assets", headers={"Authorization": "Bearer stranger"}).status_code
            == 401
        )


def test_upload_resume_hash_download_delete_and_csrf(app, settings):
    with TestClient(app, base_url=settings.public_url) as client:
        asset = client.post(
            "/api/assets", headers=HEADERS, json={"name": "app.aab", "size": 6}
        ).json()
        asset_id = asset["id"]
        url = f"/api/assets/{asset_id}"
        assert (
            client.put(url + "/chunks?offset=0", headers=HEADERS, content=b"abc").status_code == 200
        )
        assert (
            client.put(url + "/chunks?offset=0", headers=HEADERS, content=b"abc").status_code == 400
        )
        assert client.get(url, headers=HEADERS).json()["offset"] == 3
        assert (
            client.put(url + "/chunks?offset=3", headers=HEADERS, content=b"def").status_code == 200
        )
        assert (
            client.post(url + "/complete", headers=HEADERS, json={"sha256": "wrong"}).status_code
            == 400
        )
        asset = client.post(
            url + "/complete",
            headers=HEADERS,
            json={"sha256": hashlib.sha256(b"abcdef").hexdigest()},
        ).json()
        assert asset["status"] == "ready"
        link = app.state.assets.download_link(asset_id)
        assert client.get(link).content == b"abcdef"
        other = client.post(
            "/api/assets", headers=HEADERS, json={"name": "other.apk", "size": 0}
        ).json()
        assert client.get(link.replace(asset_id, other["id"])).status_code == 401
        assert (
            client.put(url + "/chunks?offset=0", headers=HEADERS, content=b"toolarge").status_code
            == 413
        )
        client.cookies.set("bridge_session", signed(OWNER, settings, "portal", 3600))
        assert client.post("/api/assets", json={"name": "csrf.apk", "size": 0}).status_code == 401
        assert (
            client.post(
                "/api/assets",
                headers={"Origin": settings.public_url},
                json={"name": "ok.apk", "size": 0},
            ).status_code
            == 200
        )
        assert client.delete(url, headers={"Origin": settings.public_url}).status_code == 200
        assert client.get(link).status_code == 404


def test_credentials_admin_only(app, settings):
    with TestClient(app, base_url=settings.public_url) as client:
        assert (
            client.post(
                "/credentials", headers={"Authorization": "Bearer reader"}, json={}
            ).status_code
            == 403
        )
        assert client.post("/credentials", headers=HEADERS, json={}).status_code == 400
        with patch.object(PlayStoreClient, "_get_service", return_value=MagicMock()):
            response = client.post(
                "/credentials", headers=HEADERS, json={"credentials": {"type": "service_account"}}
            )
        assert response.status_code == 200


@pytest.mark.parametrize("name", ["../secret.apk", "/etc/passwd", "..", "a\\b", "bad\x00.apk"])
def test_upload_paths_rejected(assets, name):
    with pytest.raises(ValueError):
        assets.create(name, 1, "owner")


def test_assets_quota_incomplete_and_confinement(assets):
    with pytest.raises(ValueError):
        assets.create("huge.apk", 101, "owner")
    asset = assets.create("app.apk", 6, "owner")
    with pytest.raises(ValueError):
        assets.finish(asset["id"])
    with pytest.raises(ValueError):
        assets.resolve(asset["reference"])
    with pytest.raises(ValueError):
        assets.resolve("/etc/passwd")
    with pytest.raises(ValueError):
        assets.path("../", "file")
    with pytest.raises(ValueError):
        assets.path("a" * 32, "../secret")


def test_upload_recovers_uncommitted_tail(assets):
    asset = assets.create("app.apk", 6, "owner")
    assets.append(asset["id"], 0, b"abc")
    with assets.path(asset["id"], asset["name"]).open("ab") as file:
        file.write(b"orphan-tail")
    assets.append(asset["id"], 3, b"def")
    assert assets.finish(asset["id"])["size"] == 6


@pytest.mark.parametrize(
    "headers",
    [
        {"x-google-credentials": "null"},
        {"x-google-credentials": "{}"},
        {"x-google-credentials-base64": "invalid"},
    ],
)
def test_bad_per_request_credentials_never_use_shared_client(runtime, headers):
    core._shared_state["client"] = MagicMock()
    with pytest.raises(PlayStoreClientError):
        runtime.client(headers)


def test_runtime_remote_upload_uses_original_tool_and_original_result(runtime, assets, monkeypatch):
    asset = assets.create("app.aab", 3, "owner")
    assets.append(asset["id"], 0, b"abc")
    assets.finish(asset["id"])
    original = TOOLS["deploy_app"]
    called = MagicMock(return_value={"success": True, "version_code": 42})
    called.__signature__ = __import__("inspect").signature(original)
    monkeypatch.setitem(TOOLS, "deploy_app", called)
    core._shared_state["client"] = MagicMock()
    result = runtime.call(
        "deploy_app",
        {"package_name": "com.example.app", "track": "internal", "file_path": asset["reference"]},
        "owner",
    )
    assert result == {"success": True, "version_code": 42}
    assert called.call_args.kwargs["file_path"] == str(assets.path(asset["id"], "app.aab"))


def test_every_original_write_blocked_in_remote_read_only(settings, assets):
    runtime = Runtime(replace(settings, read_only=True), assets)
    # Use the original suite's complete argument table for all 73 writes.
    from tests.test_read_only import WRITE_TOOLS

    assert {name for name, _ in WRITE_TOOLS} == WRITES
    with patch.object(runtime, "client", side_effect=AssertionError("Must not access Google")):
        for name, arguments in WRITE_TOOLS:
            assert "read-only" in runtime.call(name, arguments, "owner")["error"]


async def wait_job(jobs, job_id, owner):
    for _ in range(100):
        value = jobs.get(job_id, owner)
        if value["status"] not in {"queued", "running"}:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("Job did not complete")


async def test_jobs_idempotency_persistence_encryption_and_ownership(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "client", lambda _headers=None: MagicMock())
    jobs = Jobs(runtime.assets.store, runtime)
    args = {"package_name": "com.example.app"}
    with patch.object(
        runtime, "call", return_value={"success": True, "purchase_token": "sensitive-token"}
    ) as call:
        await jobs.start()
        first = await asyncio.to_thread(jobs.submit, "get_releases", args, "key", "owner")
        duplicate = jobs.submit("get_releases", args, "key", "owner")
        assert first["id"] == duplicate["id"]
        with pytest.raises(ValueError):
            jobs.submit("get_releases", {"package_name": "com.other.app"}, "key", "owner")
        result = await wait_job(jobs, first["id"], "owner")
        assert result["status"] == "completed"
        assert call.call_count == 1
        with pytest.raises(ValueError):
            jobs.get(first["id"], "other")
        await jobs.stop()
    reopened = Store(runtime.settings.data_dir, runtime.settings.signing_key)
    assert (
        Jobs(reopened, runtime).get(first["id"], "owner")["result"]["purchase_token"]
        == "sensitive-token"
    )
    assert b"sensitive-token" not in reopened.path.read_bytes()


async def test_jobs_restart_and_failed_write_never_replayed(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "client", lambda _headers=None: MagicMock())
    jobs = Jobs(runtime.assets.store, runtime)
    job = jobs.submit(
        "refund_order",
        {"package_name": "com.example.app", "order_id": "GPA.test"},
        "refund",
        "owner",
    )
    with jobs.store.connect() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    with patch.object(
        runtime, "call", return_value={"success": False, "error": "network failure"}
    ) as call:
        await jobs.start()
        assert jobs.get(job["id"], "owner")["status"] == "unknown"
        assert call.call_count == 0
        next_job = jobs.submit(
            "refund_order",
            {"package_name": "com.example.app", "order_id": "GPA.other"},
            "other-refund",
            "owner",
        )
        result = await wait_job(jobs, next_job["id"], "owner")
        assert result["status"] == "unknown"
        await jobs.stop()


@pytest.mark.parametrize(
    "change",
    [
        {"public_url": "http://example.com"},
        {"public_url": "https://example.com/mcp"},
        {"signing_key": "short"},
        {"allowed_emails": frozenset()},
        {"max_file_bytes": 0},
        {"admin_emails": frozenset({"other@example.com"})},
    ],
)
def test_invalid_production_config_rejected(settings, change):
    with pytest.raises(ValueError):
        replace(settings, **change)


def test_bounded_download_never_publishes_partial_bytes(tmp_path):
    from playstore_mcp_bridge.downloads import BoundedDownloadClient

    client = BoundedDownloadClient(download_dir=str(tmp_path))
    client.byte_limit = 3
    target = tmp_path / "app.apk"
    target.write_bytes(b"original")

    def downloader(file, request, chunksize):
        assert request is not None
        assert chunksize == 1024**2
        value = MagicMock()

        def next_chunk():
            file.write(b"oversized")
            return None, True

        value.next_chunk.side_effect = next_chunk
        return value

    with (
        patch("playstore_mcp_bridge.downloads.MediaIoBaseDownload", side_effect=downloader),
        pytest.raises(PlayStoreClientError, match="quota"),
    ):
        client._download_to_file(MagicMock(), str(target))
    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob("*.part"))


def test_interrupted_download_scratch_is_recovered(assets):
    asset = assets.create("app.apk", 20, "owner", status="downloading")
    target = assets.path(asset["id"], asset["name"])
    partial = target.parent / ".app.apk.crash.part"
    partial.write_bytes(b"partial-data")
    assets.recover_downloads()
    assert assets.get(asset["id"])["status"] == "failed"
    assert not partial.exists()
    assert assets.get(asset["id"])["size"] == 0


def test_read_only_still_blocks_code_mode_over_http(settings, monkeypatch):
    monkeypatch.setattr(PlayStoreClient, "_get_service", lambda _self: MagicMock())
    provider = StaticTokenVerifier(tokens={"owner": {"client_id": "owner", "scopes": [], **OWNER}})
    app = create_app(
        replace(settings, read_only=True), auth_provider=provider, redis_client=FakeRedis()
    )
    with TestClient(app, base_url=settings.public_url) as client:
        with patch.object(
            PlayStoreClient, "refund_order", side_effect=AssertionError("Must not call Google")
        ):
            result = rpc(
                client,
                "tools/call",
                {
                    "name": "execute",
                    "arguments": {
                        "code": "return await call_tool('refund_order', {'package_name':'com.example.app','order_id':'GPA.test'})"
                    },
                },
                path="/mcp-code",
            )
        assert "read-only" in json.dumps(result).lower()


async def test_legacy_sse_authenticated_endpoint_announces_messages(app):
    # Capture the first SSE frame and cancel before the stream waits forever.
    sent = []
    first_event = asyncio.Event()

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and b"event: endpoint" in message.get(
            "body", b""
        ):
            first_event.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/sse",
        "raw_path": b"/sse",
        "query_string": b"",
        "root_path": "",
        "server": ("bridge.example.com", 443),
        "client": ("127.0.0.1", 12345),
        "headers": [(b"host", b"bridge.example.com"), (b"authorization", b"Bearer owner")],
    }
    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(first_event.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert next(m for m in sent if m["type"] == "http.response.start")["status"] == 200
    assert any(b"/messages/" in m.get("body", b"") for m in sent)
