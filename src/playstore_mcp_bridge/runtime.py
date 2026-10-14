"""Adapters around the original functions; one registry is used by every transport."""

from __future__ import annotations

import base64
import contextvars
import inspect
import json
import threading
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp.tools import Tool
from jsonschema import validate as validate_schema  # type: ignore[import-untyped]

from play_store_mcp import server as core
from play_store_mcp.client import PlayStoreClient, PlayStoreClientError

from .downloads import BoundedDownloadClient

if TYPE_CHECKING:
    from .assets import Assets
    from .config import Settings

MANIFEST = json.loads(Path(__file__).with_name("tool_manifest.json").read_text())
TOOLS = {entry["name"]: getattr(core, entry["name"]) for entry in MANIFEST}
WRITES = {entry["name"] for entry in MANIFEST if entry["mutates_play"]}
DOWNLOADS = {"download_generated_apk", "download_system_apk_variant"}
VALIDATORS = {"validate_package_name", "validate_track", "validate_listing_text"}
CLIENT: contextvars.ContextVar[PlayStoreClient | None] = contextvars.ContextVar(
    "bridge_play_client", default=None
)
_original_client_resolver = core.get_client_from_context


def scoped_client() -> PlayStoreClient:
    client = CLIENT.get()
    return client if client is not None else _original_client_resolver()


class Runtime:
    def __init__(self, settings: Settings, assets: Assets) -> None:
        self.settings, self.assets = settings, assets
        self.download_lock = threading.Lock()
        self.locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)
        self.schemas = {name: Tool.from_function(fn).parameters for name, fn in TOOLS.items()}
        core.set_read_only(settings.read_only)
        core.get_client_from_context = scoped_client

    def client(self, headers: dict[str, str] | None = None) -> PlayStoreClient:
        headers = headers or {}
        info: Any = None
        try:
            if "x-google-credentials" in headers:
                info = json.loads(headers["x-google-credentials"])
            elif "x-google-credentials-base64" in headers:
                info = json.loads(
                    base64.b64decode(headers["x-google-credentials-base64"], validate=True).decode()
                )
        except (ValueError, UnicodeError) as exc:
            raise PlayStoreClientError("Invalid per-request Google credentials") from exc
        if "x-google-credentials" in headers or "x-google-credentials-base64" in headers:
            if not isinstance(info, dict) or not info:
                raise PlayStoreClientError(
                    "Per-request credentials must be a service account object"
                )
            return PlayStoreClient(credentials_json=info, download_dir=str(self.assets.root))
        shared = core._shared_state.get("client")
        if shared is None:
            raise PlayStoreClientError("Google Play service account is not configured")
        shared._download_dir = str(self.assets.root)
        return shared

    def validate(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in TOOLS:
            raise ValueError("Unknown operation; choose one of the 117 original tools")
        bound = inspect.signature(TOOLS[name]).bind(**arguments)
        bound.apply_defaults()
        result = dict(bound.arguments)
        validate_schema(result, self.schemas[name])
        return result

    def call(
        self,
        name: str,
        arguments: dict[str, Any],
        owner: str,
        client: PlayStoreClient | None = None,
    ) -> Any:
        args = self.validate(name, arguments)
        if self.settings.read_only and name in WRITES:
            return {"error": core.READ_ONLY_ERROR}
        active_client = client or (PlayStoreClient() if name in VALIDATORS else self.client())
        download_asset: dict[str, Any] | None = None
        if name in DOWNLOADS:
            filename = Path(args["destination_path"]).name
            if not filename.lower().endswith(".apk"):
                raise ValueError("destination_path must name an .apk file")
            download_asset = self.assets.create(filename, 0, owner, status="downloading")
            args["destination_path"] = str(self.assets.path(download_asset["id"], filename))
        else:
            for key in ("file_path", "apk_path", "bundle_path", "image_path"):
                if key in args:
                    args[key] = self.assets.resolve(args[key])
        token = CLIENT.set(active_client)
        try:
            # Per-asset locks prevent deletion during upload, while SQLite
            # remains available to status requests throughout a long operation.
            with ExitStack() as stack:
                if name in DOWNLOADS:
                    assert download_asset is not None
                    stack.enter_context(self.download_lock)
                    with self.assets.store.connect() as db:
                        used = db.execute("SELECT COALESCE(SUM(size),0) FROM assets").fetchone()[0]
                    remaining = min(
                        self.settings.max_file_bytes, self.settings.max_storage_bytes - used
                    )
                    if remaining <= 0:
                        raise ValueError("Storage quota exceeded")
                    with self.assets.store.connect() as db:
                        db.execute(
                            "UPDATE assets SET size=? WHERE id=?", (remaining, download_asset["id"])
                        )
                    active_client = BoundedDownloadClient.from_client(
                        active_client, self.assets.root, remaining
                    )
                    CLIENT.set(active_client)
                asset_ids = set()
                for key in (
                    "file_path",
                    "apk_path",
                    "bundle_path",
                    "image_path",
                    "destination_path",
                ):
                    if key in args:
                        path = Path(args[key])
                        if path.is_relative_to(self.assets.root):
                            asset_ids.add(path.parent.name)
                for asset_id in sorted(asset_ids):
                    stack.enter_context(self.assets.locks[asset_id])
                stack.enter_context(self.locks[str(args.get("package_name", "account"))])
                result = TOOLS[name](**args)
            if download_asset:
                if result.get("success"):
                    asset = self.assets.finish(download_asset["id"])
                    result.update(
                        asset_id=asset["id"], download_url=self.assets.download_link(asset["id"])
                    )
                else:
                    self.assets.delete(download_asset["id"])
            return result
        except Exception:
            if download_asset:
                self.assets.delete(download_asset["id"])
            raise
        finally:
            CLIENT.reset(token)
