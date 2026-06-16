"""Session lifecycle manager — create, activate, close, idle/max enforcement."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from server.config import AppConfig
from server.logging import get_logger
from server.storage.database import Database
from server.ws.connection_manager import ConnectionManager

log = get_logger("server.sessions")


class SessionManager:
    """Manages session state machine: created → active → closed."""

    def __init__(self, db: Database, config: AppConfig, connection_manager: ConnectionManager):
        self._db = db
        self._config = config
        self._cm = connection_manager
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start background cleanup loop."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        """Stop background cleanup loop."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    async def create_session(self, user_id: str, device_id: str) -> dict:
        """Create a new session. Validates user owns device and device is online."""
        row = await self._db.fetchone(
            "SELECT id, user_id FROM devices WHERE id = ? AND is_active = 1",
            (device_id,),
        )
        if row is None:
            raise SessionError("DEVICE_NOT_FOUND", "Device not found or inactive")
        if row["user_id"] != user_id:
            raise SessionError("DEVICE_NOT_OWNED", "Device does not belong to this user")

        if not self._cm.is_connected(device_id):
            raise SessionError("DEVICE_OFFLINE", "Device is not connected")

        active_count = await self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM sessions WHERE device_id = ? AND state = 'active'",
            (device_id,),
        )
        if active_count and active_count["cnt"] >= self._config.session.max_per_device:
            raise SessionError("DEVICE_BUSY", "Too many active sessions on this device")

        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        await self._db.execute(
            """INSERT INTO sessions
               (id, user_id, device_id, state, created_at, activated_at, last_activity_at)
               VALUES (?, ?, ?, 'active', ?, ?, ?)""",
            (session_id, user_id, device_id, now, now, now),
        )

        await self._notify_device(device_id, {
            "type": "session_create",
            "session_id": session_id,
            "user_id": user_id,
        })

        return {
            "id": session_id,
            "user_id": user_id,
            "device_id": device_id,
            "state": "active",
            "created_at": now,
            "activated_at": now,
            "last_activity_at": now,
        }

    async def get_session(self, session_id: str) -> dict | None:
        """Get session by ID."""
        row = await self._db.fetchone(
            """SELECT id, user_id, device_id, state, created_at, activated_at,
                      closed_at, last_activity_at, close_reason
               FROM sessions WHERE id = ?""",
            (session_id,),
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "device_id": row["device_id"],
            "state": row["state"],
            "created_at": row["created_at"],
            "activated_at": row["activated_at"],
            "closed_at": row["closed_at"],
            "last_activity_at": row["last_activity_at"],
            "close_reason": row["close_reason"],
        }

    async def close_session(self, session_id: str, reason: str = "user_closed") -> dict | None:
        """Close an active session. Notifies the extension."""
        session = await self.get_session(session_id)
        if session is None:
            return None
        if session["state"] == "closed":
            return session

        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """UPDATE sessions SET state = 'closed', closed_at = ?, close_reason = ?
               WHERE id = ? AND state != 'closed'""",
            (now, reason, session_id),
        )

        await self._notify_device(session["device_id"], {
            "type": "session_close",
            "session_id": session_id,
            "reason": reason,
        })

        session["state"] = "closed"
        session["closed_at"] = now
        session["close_reason"] = reason
        return session

    async def touch_session(self, session_id: str) -> None:
        """Update last_activity_at for an active session."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE sessions SET last_activity_at = ? WHERE id = ? AND state = 'active'",
            (now, session_id),
        )

    async def _cleanup_loop(self) -> None:
        """Periodically close idle and expired sessions."""
        try:
            while True:
                await asyncio.sleep(30)
                await self._close_expired_sessions()
        except asyncio.CancelledError:
            pass

    async def _close_expired_sessions(self) -> None:
        """Close sessions that exceed idle timeout or max lifetime."""
        idle_timeout = self._config.session.idle_timeout
        max_lifetime = self._config.session.max_lifetime

        rows = await self._db.fetchall(
            "SELECT id, created_at, last_activity_at FROM sessions WHERE state = 'active'"
        )

        now = datetime.now(timezone.utc)
        for row in rows:
            created = datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc)
            last_activity = datetime.fromisoformat(row["last_activity_at"]).replace(
                tzinfo=timezone.utc
            )

            if (now - last_activity).total_seconds() > idle_timeout:
                await self.close_session(row["id"], reason="idle_timeout")
            elif (now - created).total_seconds() > max_lifetime:
                await self.close_session(row["id"], reason="max_lifetime")

    async def _notify_device(self, device_id: str, message: dict) -> None:
        """Send a message to a connected device via WebSocket."""
        conn = self._cm.get(device_id)
        if conn is None:
            return
        try:
            await conn.websocket.send_text(json.dumps(message))
        except Exception as e:
            log.debug("notify_device_failed", device_id=device_id, error=str(e))


class SessionError(Exception):
    """Session operation error with error code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)
