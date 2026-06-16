"""Append-only audit logger with SHA-256 hash chain."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

from server.storage.database import Database


class AuditLogger:
    """Logs events to the audit_log table with integrity hash chain."""

    def __init__(self, db: Database):
        self._db = db
        self._lock = asyncio.Lock()

    async def log(
        self,
        event_type: str,
        *,
        actor_id: str | None = None,
        session_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        """Append an audit entry with hash chain integrity."""
        now = datetime.now(timezone.utc).isoformat()
        detail_json = json.dumps(detail or {}, sort_keys=True)

        async with self._lock:
            async with self._db.transaction() as conn:
                cursor = await conn.execute(
                    "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
                )
                row = await cursor.fetchone()
                prev_hash = row["entry_hash"] if row else None

                entry_payload = json.dumps(
                    {
                        "event_type": event_type,
                        "actor_id": actor_id,
                        "session_id": session_id,
                        "detail": detail_json,
                        "timestamp": now,
                        "prev_hash": prev_hash,
                    },
                    sort_keys=True,
                )
                entry_hash = hashlib.sha256(
                    ((prev_hash or "") + entry_payload).encode()
                ).hexdigest()

                await conn.execute(
                    """INSERT INTO audit_log
                       (event_type, actor_id, session_id, detail, prev_hash, entry_hash, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (event_type, actor_id, session_id, detail_json, prev_hash, entry_hash, now),
                )

    async def query(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Query audit entries with pagination."""
        rows = await self._db.fetchall(
            """SELECT id, event_type, actor_id, session_id, detail,
                      prev_hash, entry_hash, created_at
               FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        return [
            {
                "id": r["id"],
                "event_type": r["event_type"],
                "actor_id": r["actor_id"],
                "session_id": r["session_id"],
                "detail": json.loads(r["detail"]) if r["detail"] else {},
                "prev_hash": r["prev_hash"],
                "entry_hash": r["entry_hash"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
