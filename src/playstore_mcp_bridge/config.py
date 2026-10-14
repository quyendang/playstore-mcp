"""Validated deployment settings; production authentication is mandatory."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    public_url: str
    signing_key: str
    oauth_client_id: str
    oauth_client_secret: str
    redis_url: str
    allowed_emails: frozenset[str] = field(default_factory=frozenset)
    allowed_subjects: frozenset[str] = field(default_factory=frozenset)
    admin_emails: frozenset[str] = field(default_factory=frozenset)
    data_dir: Path = Path("/data")
    read_only: bool = False
    code_mode: bool = True
    max_file_bytes: int = 8 * 1024**3
    max_chunk_bytes: int = 8 * 1024**2
    max_storage_bytes: int = 32 * 1024**3
    max_pending_jobs: int = 100
    download_ttl: int = 900

    def __post_init__(self) -> None:
        parsed = urlsplit(self.public_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValueError("BRIDGE_PUBLIC_URL must be an HTTPS origin without a path")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("BRIDGE_PUBLIC_URL must be a plain HTTPS origin")
        if len(self.signing_key) < 32:
            raise ValueError("BRIDGE_SIGNING_KEY must contain at least 32 characters")
        if not self.oauth_client_id or not self.oauth_client_secret or not self.redis_url:
            raise ValueError("Google OAuth client ID/secret and REDIS_URL are required")
        if not self.allowed_emails and not self.allowed_subjects:
            raise ValueError("BRIDGE_ALLOWED_EMAILS or BRIDGE_ALLOWED_SUBJECTS is required")
        if not self.admin_emails <= self.allowed_emails:
            raise ValueError("Every BRIDGE_ADMIN_EMAIL must also be allowed")
        for name in (
            "max_file_bytes",
            "max_chunk_bytes",
            "max_storage_bytes",
            "max_pending_jobs",
            "download_ttl",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_env(cls) -> Settings:
        def values(name: str) -> frozenset[str]:
            return frozenset(v.strip().lower() for v in os.getenv(name, "").split(",") if v.strip())

        return cls(
            public_url=os.getenv("BRIDGE_PUBLIC_URL", "").rstrip("/"),
            signing_key=os.getenv("BRIDGE_SIGNING_KEY", ""),
            oauth_client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID", ""),
            oauth_client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            redis_url=os.getenv("REDIS_URL", ""),
            allowed_emails=values("BRIDGE_ALLOWED_EMAILS"),
            allowed_subjects=values("BRIDGE_ALLOWED_SUBJECTS"),
            admin_emails=values("BRIDGE_ADMIN_EMAILS"),
            data_dir=Path(os.getenv("BRIDGE_DATA_DIR", "/data")),
            read_only=truthy(os.getenv("PLAY_STORE_MCP_READ_ONLY", "0")),
            code_mode=truthy(os.getenv("BRIDGE_CODE_MODE", "1")),
            max_file_bytes=int(os.getenv("BRIDGE_MAX_FILE_BYTES", str(8 * 1024**3))),
            max_chunk_bytes=int(os.getenv("BRIDGE_MAX_CHUNK_BYTES", str(8 * 1024**2))),
            max_storage_bytes=int(os.getenv("BRIDGE_MAX_STORAGE_BYTES", str(32 * 1024**3))),
            max_pending_jobs=int(os.getenv("BRIDGE_MAX_PENDING_JOBS", "100")),
            download_ttl=int(os.getenv("BRIDGE_DOWNLOAD_TTL", "900")),
        )
