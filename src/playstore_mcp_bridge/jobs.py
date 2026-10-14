"""Persistent background operations with explicit ambiguous-outcome handling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from .runtime import WRITES, Runtime

if TYPE_CHECKING:
    from .storage import Store


class Jobs:
    def __init__(self, store: Store, runtime: Runtime) -> None:
        self.store, self.runtime = store, runtime
        self.wakeup = asyncio.Event()
        self.worker: asyncio.Task[None] | None = None
        self.stopping = False
        self.loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        with self.store.connect() as db:
            # Never replay an operation that may have reached Google before a crash.
            db.execute(
                "UPDATE jobs SET status='unknown',error='Server restarted during execution; verify the outcome in Google Play before resubmitting',updated=? WHERE status='running'",
                (time.time(),),
            )
        self.worker = asyncio.create_task(self.run(), name="playstore-operation-worker")
        self.wakeup.set()

    async def stop(self) -> None:
        self.stopping = True
        self.wakeup.set()
        if self.worker:
            # Allow the current call to complete during graceful shutdown. A hard
            # kill leaves it 'running', which next startup marks 'unknown'.
            await self.worker

    def submit(
        self, tool_name: str, arguments: dict[str, Any], idempotency_key: str, owner: str
    ) -> dict[str, Any]:
        args = self.runtime.validate(tool_name, arguments)
        if self.runtime.settings.read_only and tool_name in WRITES:
            raise ValueError("Write operations are disabled in read-only mode")
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("Provide an idempotency_key of 1 to 200 characters")
        self.runtime.client()  # Background jobs use the configured shared service account.
        payload_hash = hashlib.sha256(
            json.dumps([tool_name, args], sort_keys=True).encode()
        ).hexdigest()
        with self.store.connect() as db:
            existing = db.execute(
                "SELECT id,request_hash FROM jobs WHERE owner=? AND idempotency_key=?",
                (owner, idempotency_key),
            ).fetchone()
            if existing:
                if existing["request_hash"] != payload_hash:
                    raise ValueError(
                        "This idempotency key was already used for different arguments"
                    )
                return self.get(existing["id"], owner)
            pending = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()[0]
            if pending >= self.runtime.settings.max_pending_jobs:
                raise ValueError("Operation queue is full")
            now, job_id = time.time(), uuid.uuid4().hex
            db.execute(
                "INSERT INTO jobs(id,owner,tool,status,created,updated,idempotency_key,request_hash,arguments) VALUES(?,?,?,'queued',?,?,?,?,?)",
                (
                    job_id,
                    owner,
                    tool_name,
                    now,
                    now,
                    idempotency_key,
                    payload_hash,
                    self.store.encrypt(args),
                ),
            )
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.wakeup.set)
        return self.get(job_id, owner)

    def get(self, job_id: str, owner: str) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)
            ).fetchone()
        if row is None:
            raise ValueError("Operation not found")
        result = {key: row[key] for key in ("id", "tool", "status", "created", "updated", "error")}
        if row["result"] is not None:
            result["result"] = self.store.decrypt(row["result"])
        return result

    def list(self, owner: str, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id FROM jobs WHERE owner=? ORDER BY created DESC LIMIT ?", (owner, limit)
            ).fetchall()
        return [self.get(row["id"], owner) for row in rows]

    async def run(self) -> None:
        while not self.stopping:
            self.wakeup.clear()
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
                ).fetchone()
                if row:
                    db.execute(
                        "UPDATE jobs SET status='running',updated=? WHERE id=?",
                        (time.time(), row["id"]),
                    )
            if not row:
                await self.wakeup.wait()
                continue
            try:
                args = self.store.decrypt(row["arguments"])
                result = await asyncio.to_thread(self.runtime.call, row["tool"], args, row["owner"])
                failure = isinstance(result, dict) and (
                    result.get("success") is False or bool(result.get("error"))
                )
                status = (
                    ("unknown" if row["tool"] in WRITES else "failed") if failure else "completed"
                )
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE jobs SET status=?,result=?,updated=? WHERE id=?",
                        (status, self.store.encrypt(result), time.time(), row["id"]),
                    )
            except Exception as exc:
                # Exceptions can follow a successful network mutation. Do not
                # label it safe to retry or include secret-bearing error strings.
                status = "unknown" if row["tool"] in WRITES else "failed"
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE jobs SET status=?,error=?,updated=? WHERE id=?",
                        (
                            status,
                            f"Operation raised {type(exc).__name__}; check Google Play before retrying writes",
                            time.time(),
                            row["id"],
                        ),
                    )
