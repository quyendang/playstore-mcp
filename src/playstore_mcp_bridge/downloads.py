"""Google media downloads with bounded disk usage and atomic publication."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any

from googleapiclient.http import MediaIoBaseDownload

from play_store_mcp.client import PlayStoreClient, PlayStoreClientError


class BoundedDownloadClient(PlayStoreClient):
    byte_limit: int

    @classmethod
    def from_client(cls, client: PlayStoreClient, root: Path, limit: int) -> BoundedDownloadClient:
        instance = cls(
            credentials_path=client._credentials_path,
            credentials_json=client._credentials_json,
            download_dir=str(root),
        )
        instance._service = client._service
        instance._service_lock = client._service_lock
        instance._http_lock = client._http_lock
        instance.byte_limit = limit
        return instance

    def _download_to_file(self, request: Any, destination_path: str) -> None:
        safe = Path(self._confine_download_path(destination_path))
        fd, temporary = tempfile.mkstemp(dir=safe.parent, prefix=f".{safe.name}.", suffix=".part")
        published = False
        try:
            with os.fdopen(fd, "wb") as file:
                download = MediaIoBaseDownload(file, request, chunksize=1024**2)
                done = False
                while not done:
                    with self._http_lock:
                        _, done = download.next_chunk()
                    if file.tell() > self.byte_limit:
                        raise PlayStoreClientError(
                            "Download exceeds the configured file or storage quota"
                        )
                file.flush()
                os.fsync(file.fileno())
            Path(temporary).replace(safe)
            published = True
        finally:
            if not published:
                with contextlib.suppress(OSError):
                    Path(temporary).unlink()
