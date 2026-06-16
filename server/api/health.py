"""Health check endpoint — GET /api/v1/health."""

from __future__ import annotations

import time

from fastapi import APIRouter
from pydantic import BaseModel

from server.storage.database import Database
from server.ws.connection_manager import ConnectionManager

router = APIRouter()

_db: Database | None = None
_cm: ConnectionManager | None = None
_server_version: str = "0.0.0"
_start_time: float = time.time()


def init_health_router(
    db: Database, connection_manager: ConnectionManager, server_version: str = "0.0.0"
) -> APIRouter:
    global _db, _cm, _server_version
    _db = db
    _cm = connection_manager
    _server_version = server_version
    return router


class HealthResponse(BaseModel):
    status: str
    server_version: str
    uptime_seconds: float
    connected_devices: int
    active_sessions: int
    db_status: str


@router.get("/health", response_model=HealthResponse)
async def health_check():
    assert _db is not None
    assert _cm is not None

    db_status = "ok"
    active_sessions = 0
    try:
        row = await _db.fetchone(
            "SELECT COUNT(*) as cnt FROM sessions WHERE state = 'active'"
        )
        active_sessions = row["cnt"] if row else 0
    except Exception:
        db_status = "error"

    total_connected = _cm.connected_count

    return HealthResponse(
        status="ok",
        server_version=_server_version,
        uptime_seconds=round(time.time() - _start_time, 1),
        connected_devices=total_connected,
        active_sessions=active_sessions,
        db_status=db_status,
    )
