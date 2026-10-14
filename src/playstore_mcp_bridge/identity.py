"""Google identity allowlist shared by MCP and the file portal."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import jwt
from fastmcp.server.auth.providers.google import GoogleProvider
from pydantic import AnyHttpUrl

if TYPE_CHECKING:
    from fastmcp.server.auth import AccessToken

    from .config import Settings


class AccessDenied(ValueError):
    """An authenticated identity does not have access to this bridge."""


def allowed_identity(claims: dict[str, Any], settings: Settings) -> str:
    subject = str(claims.get("sub") or "")
    email = str(claims.get("email") or "").lower()
    verified = claims.get("email_verified") in (True, "true", "True")
    if subject and (
        subject in settings.allowed_subjects or (verified and email in settings.allowed_emails)
    ):
        return subject
    raise AccessDenied("Your Google account is not on this bridge's allowlist")


class AllowlistedGoogleProvider(GoogleProvider):
    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        self.bridge_settings = settings
        super().__init__(
            client_id=settings.oauth_client_id,
            client_secret=settings.oauth_client_secret,
            base_url=settings.public_url,
            required_scopes=["openid", "email", "profile"],
            jwt_signing_key=settings.signing_key,
            require_authorization_consent=True,
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
            **kwargs,
        )

    def _get_resource_url(self, path: str | None = None) -> AnyHttpUrl | None:  # noqa: ARG002
        # All transport aliases and file APIs are one OAuth resource. Advertise
        # the origin as their valid parent (RFC 8707 / MCP resource metadata),
        # so creating /sse or /mcp-code cannot change the /mcp token audience.
        return self.resource_base_url or self.base_url

    async def verify_token(self, token: str) -> AccessToken | None:
        # Authorize on EVERY request, including after allowlist changes.
        access = await super().verify_token(token)
        if access is None:
            return None
        try:
            allowed_identity(access.claims, self.bridge_settings)
        except AccessDenied:
            return None
        return access


def decode_signed(token: str, settings: Settings, audience: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.signing_key,
        algorithms=["HS256"],
        audience=audience,
        issuer=settings.public_url,
        options={"require": ["exp", "iat", "iss", "aud"]},
    )
