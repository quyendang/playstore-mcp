"""Resumable uploads and confined downloads for remote MCP tools."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jwt

if TYPE_CHECKING:
    from .config import Settings
    from .storage import Store


class Assets:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings, self.store = settings, store
        self.root = (settings.data_dir / "assets").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)

    def path(self, asset_id: str, name: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", asset_id):
            raise ValueError("Invalid asset ID")
        path = (self.root / asset_id / name).resolve()
        if not path.is_relative_to(self.root / asset_id):
            raise ValueError("Invalid asset path")
        return path

    def get(self, asset_id: str) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise ValueError("Asset not found")
        return {**dict(row), "reference": f"asset://{asset_id}"}

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM assets ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{**dict(row), "reference": f"asset://{row['id']}"} for row in rows]

    def create(self, name: str, size: int, owner: str, status: str = "uploading") -> dict[str, Any]:
        if (
            not name
            or name in {".", ".."}
            or Path(name).name != name
            or "\\" in name
            or len(name) > 200
            or any(ord(c) < 32 for c in name)
        ):
            raise ValueError("Provide a filename without directory components")
        if not 0 <= size <= self.settings.max_file_bytes:
            raise ValueError("File exceeds BRIDGE_MAX_FILE_BYTES")
        asset_id = uuid.uuid4().hex
        with self.store.connect() as db:
            used = db.execute("SELECT COALESCE(SUM(size), 0) FROM assets").fetchone()[0]
            if used + size > self.settings.max_storage_bytes:
                raise ValueError("Storage quota exceeded; delete unused assets")
            path = self.path(asset_id, name)
            path.parent.mkdir()
            try:
                path.touch(exist_ok=False)
                db.execute(
                    "INSERT INTO assets(id,name,size,status,created,owner) VALUES(?,?,?,?,?,?)",
                    (asset_id, name, size, status, time.time(), owner),
                )
            except Exception:
                path.unlink(missing_ok=True)
                path.parent.rmdir()
                raise
        return self.get(asset_id)

    def append(self, asset_id: str, offset: int, chunk: bytes) -> dict[str, Any]:
        if not chunk or len(chunk) > self.settings.max_chunk_bytes:
            raise ValueError("Empty or oversized upload chunk")
        with self.locks[asset_id], self.store.connect() as db:
            asset = self.get(asset_id)
            if asset["status"] != "uploading" or offset != asset["offset"]:
                raise ValueError("Upload offset mismatch; fetch the asset and resume at its offset")
            if offset + len(chunk) > asset["size"]:
                raise ValueError("Chunk exceeds declared file size")
            with self.path(asset_id, asset["name"]).open("r+b") as file:
                # Truncate any uncommitted tail after a process crash.
                file.seek(offset)
                file.write(chunk)
                file.truncate(offset + len(chunk))
                file.flush()
                os.fsync(file.fileno())
            db.execute("UPDATE assets SET offset=? WHERE id=?", (offset + len(chunk), asset_id))
        return self.get(asset_id)

    def finish(self, asset_id: str, expected_sha256: str | None = None) -> dict[str, Any]:
        with self.locks[asset_id]:
            asset = self.get(asset_id)
            if asset["status"] not in {"uploading", "downloading"}:
                if asset["status"] == "ready":
                    return asset
                raise ValueError("Asset is not uploadable")
            path = self.path(asset_id, asset["name"])
            size = path.stat().st_size
            if asset["status"] == "uploading" and (
                asset["offset"] != asset["size"] or size != asset["size"]
            ):
                raise ValueError("Upload is incomplete")
            if size > self.settings.max_file_bytes:
                raise ValueError("Downloaded file exceeds BRIDGE_MAX_FILE_BYTES")
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            if expected_sha256 and expected_sha256.lower() != digest:
                raise ValueError("SHA-256 mismatch")
            with self.store.connect() as db:
                used = db.execute(
                    "SELECT COALESCE(SUM(size),0) FROM assets WHERE id<>?", (asset_id,)
                ).fetchone()[0]
                if used + size > self.settings.max_storage_bytes:
                    raise ValueError("Storage quota exceeded")
                db.execute(
                    "UPDATE assets SET status='ready',size=?,offset=?,sha256=? WHERE id=?",
                    (size, size, digest, asset_id),
                )
            return self.get(asset_id)

    def resolve(self, value: str) -> str:
        if value.startswith("asset://"):
            asset = self.get(value.removeprefix("asset://"))
            if asset["status"] != "ready":
                raise ValueError("Asset upload has not completed")
            return str(self.path(asset["id"], asset["name"]))
        path = Path(value).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError(
                "Remote file inputs must use asset://ID or a file inside the assets directory"
            )
        return str(path)

    def download_link(self, asset_id: str) -> str:
        asset = self.get(asset_id)
        if asset["status"] != "ready":
            raise ValueError("Asset is not ready")
        now = int(time.time())
        token = jwt.encode(
            {
                "asset": asset_id,
                "iat": now,
                "exp": now + self.settings.download_ttl,
                "aud": "asset-download",
                "iss": self.settings.public_url,
            },
            self.settings.signing_key,
            algorithm="HS256",
        )
        return f"{self.settings.public_url}/files/{asset_id}?token={token}"

    def recover_downloads(self) -> None:
        """Reclaim interrupted media-transfer scratch files after a restart."""
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM assets WHERE status='downloading'").fetchall()
            for row in rows:
                target = self.path(row["id"], row["name"])
                for partial in target.parent.glob(f".{target.name}.*.part"):
                    partial.unlink()
                actual_size = target.stat().st_size if target.exists() else 0
                db.execute(
                    "UPDATE assets SET status='failed',size=?,offset=? WHERE id=?",
                    (actual_size, actual_size, row["id"]),
                )

    def delete(self, asset_id: str) -> dict[str, Any]:
        with self.locks[asset_id]:
            asset = self.get(asset_id)
            target = self.path(asset_id, asset["name"])
            target.unlink(missing_ok=True)
            for partial in target.parent.glob(f".{target.name}.*.part"):
                partial.unlink()
            target.parent.rmdir()
            with self.store.connect() as db:
                db.execute("DELETE FROM assets WHERE id=?", (asset_id,))

        return {"success": True, "asset_id": asset_id}
