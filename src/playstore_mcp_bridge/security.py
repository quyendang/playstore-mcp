"""Origin checks for the aggregated ASGI transport aliases."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class OriginMiddleware:
    def __init__(self, app: ASGIApp, public_url: str) -> None:
        self.app, self.public_url = app, public_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] in {
            "/mcp",
            "/mcp-code",
            "/sse",
            "/messages",
            "/messages/",
        }:
            headers = dict(scope.get("headers", []))
            origin = headers.get(b"origin")
            if origin and origin.decode() != self.public_url:
                await JSONResponse({"error": "Origin is not allowed"}, status_code=403)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)
