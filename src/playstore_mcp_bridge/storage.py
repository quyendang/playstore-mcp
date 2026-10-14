"""Volume-backed metadata and encrypted operation records."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class Store:
    def __init__(self, root: Path, signing_key: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "bridge.sqlite3"
        self.lock = threading.RLock()
        self.cipher = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(("jobs:" + signing_key).encode()).digest())
        )
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
                    offset INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                    sha256 TEXT, created REAL NOT NULL, owner TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, tool TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                    arguments BLOB NOT NULL, result BLOB, error TEXT,
                    UNIQUE(owner, idempotency_key)
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def encrypt(self, value: Any) -> bytes:
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())

    def decrypt(self, value: bytes) -> Any:
        return json.loads(self.cipher.decrypt(value))

    def ready(self) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT 1").fetchone()[0] == 1)
