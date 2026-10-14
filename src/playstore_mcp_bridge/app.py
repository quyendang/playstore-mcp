"""Railway ASGI entry point with OAuth, all upstream tools and remote file/job APIs."""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import inspect
import json
import logging
import os
import sqlite3
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import jwt
import uvicorn
from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.experimental.transforms.code_mode import CodeMode
from fastmcp.server.dependencies import get_access_token, get_http_headers
from fastmcp.tools import Tool
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.types import ToolAnnotations
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from play_store_mcp import server as core
from play_store_mcp.client import PlayStoreClient, PlayStoreClientError

from . import portal
from .assets import Assets
from .config import Settings
from .identity import AccessDenied, AllowlistedGoogleProvider, allowed_identity, decode_signed
from .jobs import Jobs
from .runtime import DOWNLOADS, TOOLS, VALIDATORS, WRITES, Runtime
from .security import OriginMiddleware
from .storage import Store

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from fastmcp.server.auth import AuthProvider
    from starlette.requests import Request

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings,
    *,
    auth_provider: AuthProvider | None = None,
    redis_client: Any = None,
    oauth_store: Any = None,
) -> Starlette:
    redis = (
        redis_client
        if redis_client is not None
        else Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=10, socket_timeout=10
        )
    )
    if auth_provider is None:
        backing_store = (
            oauth_store
            if oauth_store is not None
            else RedisStore(client=redis, default_collection="playstore-oauth")
        )
        encrypted_store = FernetEncryptionWrapper(
            backing_store,
            fernet=Fernet(
                base64.urlsafe_b64encode(
                    hashlib.sha256(("oauth:" + settings.signing_key).encode()).digest()
                )
            ),
        )
        auth_provider = AllowlistedGoogleProvider(settings, client_storage=encrypted_store)
    provider = auth_provider
    store = Store(settings.data_dir, settings.signing_key)
    assets = Assets(settings, store)
    runtime = Runtime(settings, assets)
    jobs = Jobs(store, runtime)

    def actor() -> str:
        access = get_access_token()
        if access is None:
            raise AccessDenied("Authentication required")
        return allowed_identity(access.claims, settings)

    def adapter(name: str) -> Callable[..., Awaitable[Any]]:
        original = TOOLS[name]

        @functools.wraps(original)
        async def invoke(**kwargs: Any) -> Any:
            owner = actor()
            headers = get_http_headers() or {}
            client = None if name in VALIDATORS else runtime.client(headers)
            return await asyncio.to_thread(runtime.call, name, kwargs, owner, client)

        invoke.__signature__ = inspect.signature(original)  # type: ignore[attr-defined]
        return invoke

    mcp = FastMCP(
        "Play Store Bridge",
        auth=provider,
        instructions="Manage Google Play using the complete original tool set. For remote file inputs use asset://ID from the file portal. For large uploads/deploys/downloads use start_operation then get_operation; use a unique idempotency key per intended operation. An unknown operation outcome requires checking Google Play before retrying. All allowed users share the configured Play account and file library.",
    )
    registered: list[Tool] = []

    def register_tool(
        *, annotations: dict[str, Any]
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool = Tool.from_function(fn, annotations=ToolAnnotations.model_validate(annotations))
            registered.append(tool)
            mcp.add_tool(tool)
            return fn

        return decorate

    for name in TOOLS:
        mutates = name in WRITES
        destructive = mutates and (
            name.startswith(("delete_", "batch_delete_", "refund_", "revoke_", "cancel_"))
            or name in {"set_data_safety", "update_user", "update_grant"}
        )
        tool = Tool.from_function(
            adapter(name),
            annotations=ToolAnnotations(
                read_only_hint=not mutates and name not in DOWNLOADS,
                destructive_hint=destructive,
                idempotent_hint=not mutates and name not in DOWNLOADS,
                open_world_hint=False,
            ),
        )
        registered.append(tool)
        mcp.add_tool(tool)

    @register_tool(
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}
    )
    async def start_operation(
        tool_name: str, arguments: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        """Queue one original tool for durable background execution. Use for large uploads, deploys, downloads or batches. Background jobs use the server's shared Google Play account. Reusing the same key and arguments returns the same job; never retry an unknown outcome without checking Google Play."""
        actor_id = actor()
        headers = get_http_headers() or {}
        if "x-google-credentials" in headers or "x-google-credentials-base64" in headers:
            raise ValueError(
                "Background operations use server credentials; use a direct tool for per-request credentials"
            )
        return await asyncio.to_thread(jobs.submit, tool_name, arguments, idempotency_key, actor_id)

    @register_tool(annotations={"readOnlyHint": True})
    async def get_operation(operation_id: str) -> dict[str, Any]:
        """Get your operation status and original tool result after execution or reconnect. Status unknown means verify Google Play before retrying. Download URLs expire; use get_asset to issue a fresh link."""
        return await asyncio.to_thread(jobs.get, operation_id, actor())

    @register_tool(annotations={"readOnlyHint": True})
    async def list_operations(limit: int = 50) -> list[dict[str, Any]]:
        """List your recent background operations; each includes its status and result."""
        return await asyncio.to_thread(jobs.list, actor(), limit)

    @register_tool(annotations={"readOnlyHint": True})
    async def list_assets(limit: int = 100) -> list[dict[str, Any]]:
        """List shared files and asset:// references. Upload files at the bridge's /portal page or through its authenticated /api/assets endpoints."""
        actor()
        return await asyncio.to_thread(assets.list, limit)

    @register_tool(annotations={"readOnlyHint": True})
    async def get_asset(asset_id: str) -> dict[str, Any]:
        """Get a file's metadata, upload offset, SHA-256 and a fresh download link when ready."""
        actor()
        result = await asyncio.to_thread(assets.get, asset_id)
        if result["status"] == "ready":
            result["download_url"] = assets.download_link(asset_id)
        return result

    @register_tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def delete_asset(asset_id: str) -> dict[str, Any]:
        """Permanently delete a shared server file. This deletes local storage only and does not change Google Play."""
        actor()
        return await asyncio.to_thread(assets.delete, asset_id)

    primary = mcp.http_app(path="/mcp", stateless_http=True)
    legacy = mcp.http_app(path="/sse", transport="sse")
    code_server = None
    code_app = None
    if settings.code_mode:
        code_server = FastMCP("Play Store Bridge Code Mode", auth=provider, transforms=[CodeMode()])
        for code_tool in registered:
            code_server.add_tool(code_tool)
        code_app = code_server.http_app(path="/mcp-code", stateless_http=True)

    async def identity(request: Request, *, admin: bool = False) -> tuple[str, dict[str, Any]]:
        if portal.COOKIE in request.cookies:
            try:
                claims = decode_signed(request.cookies[portal.COOKIE], settings, "portal")
            except jwt.PyJWTError as exc:
                raise AccessDenied("Sign in again") from exc
            # Prevent cross-site mutation requests using a browser session.
            if (
                request.method not in {"GET", "HEAD", "OPTIONS"}
                and request.headers.get("origin") != settings.public_url
            ):
                raise AccessDenied("A same-origin request is required")
        else:
            header = request.headers.get("authorization", "")
            if not header.startswith("Bearer "):
                raise AccessDenied("Authentication required")
            access = getattr(request.user, "access_token", None)
            if access is None:
                access = await provider.verify_token(header[7:])
            if access is None:
                raise AccessDenied("Invalid or unauthorized access token")
            claims = access.claims
        owner = allowed_identity(claims, settings)
        if admin and str(claims.get("email", "")).lower() not in settings.admin_emails:
            raise AccessDenied("Administrator access required")
        return owner, claims

    async def api(request: Request) -> Response:
        try:
            owner, _ = await identity(request)
            asset_id = str(request.path_params.get("asset_id", ""))
            if request.method == "GET":
                value = (
                    await asyncio.to_thread(assets.get, asset_id)
                    if asset_id
                    else await asyncio.to_thread(assets.list)
                )
                return JSONResponse(value)
            if request.method == "DELETE":
                return JSONResponse(await asyncio.to_thread(assets.delete, asset_id))
            if request.method == "PUT":
                offset = int(request.query_params.get("offset", "-1"))
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > settings.max_chunk_bytes:
                        return JSONResponse({"error": "Upload chunk too large"}, status_code=413)
                    body.extend(chunk)
                return JSONResponse(
                    await asyncio.to_thread(assets.append, asset_id, offset, bytes(body))
                )
            data = await _json_body(request)
            if asset_id:
                value = await asyncio.to_thread(assets.finish, asset_id, data.get("sha256"))
            else:
                size = data.get("size")
                if isinstance(size, bool) or not isinstance(size, int):
                    raise ValueError("size must be an integer")
                value = await asyncio.to_thread(assets.create, data.get("name", ""), size, owner)
            return JSONResponse(value)
        except AccessDenied as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def files(request: Request) -> Response:
        try:
            asset_id = request.path_params["asset_id"]
            token = request.query_params.get("token")
            if token:
                claims = decode_signed(token, settings, "asset-download")
                if claims.get("asset") != asset_id:
                    raise AccessDenied("Download link does not match this asset")
            else:
                await identity(request)
            asset = await asyncio.to_thread(assets.get, asset_id)
            if asset["status"] != "ready":
                raise ValueError("File is not ready")
            return FileResponse(
                assets.path(asset_id, asset["name"]),
                filename=asset["name"],
                media_type="application/octet-stream",
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                },
            )
        except (AccessDenied, jwt.PyJWTError):
            return JSONResponse(
                {"error": "Invalid, expired, or missing download authorization"}, status_code=401
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def credentials(request: Request) -> Response:
        try:
            await identity(request, admin=True)
            data = await _json_body(request)
            client, error = core._parse_credentials_request_body(data)
            if error is not None:
                return error
            if client is None:
                raise ValueError("Missing credentials")
            await asyncio.to_thread(client._get_service)
            core._shared_state.update({"client": client, "credentials_updated": True})
            return JSONResponse(
                {
                    "success": True,
                    "message": "Shared Google Play credentials updated in memory. Configure Railway variables to persist across restarts.",
                }
            )
        except AccessDenied as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except (ValueError, PlayStoreClientError):
            return JSONResponse({"error": "Invalid credentials payload"}, status_code=400)

    async def health(_request: Request) -> Response:
        return JSONResponse(
            {"status": "healthy", "service": "playstore-mcp-bridge", "original_tools": len(TOOLS)}
        )

    async def ready(_request: Request) -> Response:
        try:
            await redis.ping()
            await asyncio.to_thread(store.ready)
            if core._shared_state.get("client") is None:
                raise ValueError("Google Play not configured")
            return JSONResponse({"status": "ready"})
        except (RedisError, OSError, sqlite3.Error, ValueError):
            return JSONResponse({"status": "not_ready"}, status_code=503)

    async def portal_route(request: Request) -> Response:
        if request.url.path == "/portal/login":
            return portal.login(settings)
        if request.url.path == "/portal/callback":
            return await portal.callback(request, settings)
        if request.url.path == "/portal/style.css":
            return Response(portal.STYLE, media_type="text/css")
        if request.url.path == "/portal/script.js":
            return Response(
                portal.script(settings.max_chunk_bytes), media_type="application/javascript"
            )
        try:
            await identity(request)
        except AccessDenied:
            return RedirectResponse("/portal/login")
        if request.url.path == "/portal/logout":
            response = RedirectResponse("/portal", status_code=303)
            response.delete_cookie(
                portal.COOKIE, path="/", secure=True, httponly=True, samesite="lax"
            )
            return response
        return portal.page()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await redis.ping()  # Fail closed if OAuth persistence is unavailable.
        previous_client = core._shared_state["client"]
        previous_updated = core._shared_state["credentials_updated"]
        client = PlayStoreClient(download_dir=str(assets.root))
        try:
            await asyncio.to_thread(client._get_service)
            core._shared_state.update({"client": client, "credentials_updated": False})
        except PlayStoreClientError:
            core._shared_state.update({"client": None, "credentials_updated": False})
            logger.warning(
                "Configure GOOGLE_PLAY_STORE_CREDENTIALS or use the administrator credentials endpoint"
            )
        await asyncio.to_thread(assets.recover_downloads)
        await jobs.start()
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(primary.lifespan(app))
                await stack.enter_async_context(legacy.lifespan(app))
                if code_app:
                    await stack.enter_async_context(code_app.lifespan(app))
                yield
        finally:
            await jobs.stop()
            core._shared_state["client"] = previous_client
            core._shared_state["credentials_updated"] = previous_updated
            await redis.aclose()

    routes = [
        Route("/", endpoint=lambda _request: RedirectResponse("/portal")),
        Route("/health", health),
        Route("/ready", ready),
        Route("/credentials", credentials, methods=["POST"]),
        Route("/portal", portal_route),
        Route("/portal/login", portal_route),
        Route("/portal/callback", portal_route),
        Route("/portal/logout", portal_route, methods=["POST"]),
        Route("/portal/style.css", portal_route),
        Route("/portal/script.js", portal_route),
        Route("/api/assets", api, methods=["GET", "POST"]),
        Route("/api/assets/{asset_id}", api, methods=["GET", "DELETE"]),
        Route("/api/assets/{asset_id}/chunks", api, methods=["PUT"]),
        Route("/api/assets/{asset_id}/complete", api, methods=["POST"]),
        Route("/files/{asset_id}", files),
        *primary.routes,
        *(route for route in legacy.routes if getattr(route, "path", "") in {"/sse", "/messages"}),
    ]
    if code_app:
        routes.extend(
            route for route in code_app.routes if getattr(route, "path", "") == "/mcp-code"
        )
    middleware = [
        Middleware(OriginMiddleware, public_url=settings.public_url),
        Middleware(
            TrustedHostMiddleware,
            allowed_hosts=[
                str(urlsplit(settings.public_url).hostname),
                "healthcheck.railway.app",
                "localhost",
                "127.0.0.1",
                "[::1]",
            ],
        ),
        *provider.get_middleware(),
    ]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.mcp, app.state.runtime, app.state.jobs, app.state.assets = mcp, runtime, jobs, assets
    app.state.code_mcp = code_server
    return app


async def _json_body(request: Request) -> dict[str, Any]:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 1024**2:
            raise ValueError("JSON request too large")
        body.extend(chunk)
    data = json.loads(body)
    if not isinstance(data, dict):
        raise TypeError("Expected a JSON object")
    return data


def main() -> None:
    settings = Settings.from_env()
    app = create_app(settings)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", os.getenv("MCP_PORT", "8000"))),
        timeout_graceful_shutdown=30,
        access_log=False,
    )


if __name__ == "__main__":
    main()
