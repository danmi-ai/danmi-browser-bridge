"""Device listing endpoint — GET /api/v1/devices."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.auth.dependencies import require_any_auth
from server.auth.validator import AuthInfo
from server.storage.database import Database
from server.ws.connection_manager import ConnectionManager

router = APIRouter()

_db: Database | None = None
_connection_manager: ConnectionManager | None = None


def init_devices_router(
    db: Database, connection_manager: ConnectionManager | None = None
) -> APIRouter:
    global _db, _connection_manager
    _db = db
    _connection_manager = connection_manager
    return router


class DeviceResponse(BaseModel):
    id: str
    name: str
    is_active: bool
    last_seen_at: str | None = None
    created_at: str
    # Parsed device meta (ext_version, user_agent, platform, ...). Previously
    # the endpoint dropped meta_json entirely, so callers always saw a null
    # ext_version even when the DB had it. `ext_version` is hoisted out for
    # convenience (it's the field most consumers want).
    ext_version: str | None = None
    meta: dict = {}


@router.get("/devices", response_model=list[DeviceResponse])
async def list_devices(auth: AuthInfo = Depends(require_any_auth)):
    assert _db is not None
    rows = await _db.fetchall(
        "SELECT id, name, is_active, last_seen_at, created_at, meta_json "
        "FROM devices WHERE user_id = ?",
        (auth.user_id,),
    )
    out: list[DeviceResponse] = []
    for row in rows:
        d = dict(row)
        meta_raw = d.pop("meta_json", "") or "{}"
        try:
            meta = json.loads(meta_raw)
            if not isinstance(meta, dict):
                meta = {}
        except (ValueError, TypeError):
            meta = {}
        out.append(
            DeviceResponse(
                **d,
                meta=meta,
                ext_version=meta.get("ext_version"),
            )
        )
    return out


@router.post("/devices/{device_id}/revoke")
async def revoke_device(device_id: str, auth: AuthInfo = Depends(require_any_auth)):
    """Revoke a device that belongs to the authenticated user."""
    assert _db is not None
    row = await _db.fetchone(
        "SELECT id, name, user_id, is_active FROM devices WHERE id = ?",
        (device_id,),
    )
    if row is None or row["user_id"] != auth.user_id:
        raise HTTPException(status_code=404, detail="Device not found")
    if not row["is_active"]:
        return {"ok": True, "already_revoked": True}
    await _db.execute(
        "UPDATE devices SET is_active = 0, updated_at = datetime('now') WHERE id = ?",
        (device_id,),
    )
    # Drop the live socket too (mirror admin revoke path) — flipping is_active=0
    # alone leaves an already-connected device able to keep serving commands
    # until the periodic revocation watcher catches up (AUTHZ-4).
    if _connection_manager:
        await _connection_manager.revoke(device_id)
    return {"ok": True, "revoked_device_id": device_id}
