"""Admin endpoints — dashboard API + config hot-reload.

Auth model: a single ``X-Admin-Token`` header. The token is generated via
``python -m server.cli create-admin-token`` and stored in plaintext at
``data/.admin_token`` (chmod 0600). We compare hashes timing-safely.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query

from server.admin_ops import (
    create_pairing_code,
    create_user,
    grant_evaluate,
    grant_network,
    revoke_device,
    revoke_evaluate,
    revoke_network,
    revoke_user,
)
from server.config import AppConfig, load_config
from server.limiter import RateLimiter
from server.logging import get_logger
from server.storage.database import Database
from server.ws.connection_manager import ConnectionManager

log = get_logger("server.admin")

router = APIRouter()

_admin_token_path: Path | None = None
_rate_limiter: RateLimiter | None = None
_connection_manager: ConnectionManager | None = None
_db: Database | None = None
_config: AppConfig | None = None
_start_time: float = time.time()


def init_admin_router(
    *,
    admin_token_path: Path,
    rate_limiter: RateLimiter,
    connection_manager: ConnectionManager | None = None,
    db: Database | None = None,
    config: AppConfig | None = None,
) -> APIRouter:
    global _admin_token_path, _rate_limiter, _connection_manager, _db, _config
    _admin_token_path = admin_token_path
    _rate_limiter = rate_limiter
    _connection_manager = connection_manager
    _db = db
    _config = config
    return router


def _check_admin(token: str | None) -> None:
    if not token:
        raise HTTPException(status_code=401, detail="X-Admin-Token header required")
    if _admin_token_path is None or not _admin_token_path.exists():
        raise HTTPException(
            status_code=503,
            detail="admin token not provisioned; run `python -m server.cli create-admin-token`",
        )
    expected = _admin_token_path.read_text().strip()
    if not expected:
        raise HTTPException(status_code=503, detail="admin token file is empty")
    # timing-safe comparison on the SHA-256 digest (avoids leaking length too)
    a = hashlib.sha256(token.encode()).digest()
    b = hashlib.sha256(expected.encode()).digest()
    if not hmac.compare_digest(a, b):
        raise HTTPException(status_code=403, detail="invalid admin token")


@router.post("/admin/reload-config")
async def reload_config(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Re-read the config TOML and refresh hot-reloadable subsystems.

    Returns the diff so an operator can sanity-check what changed.
    """
    _check_admin(x_admin_token)

    try:
        new_config = load_config()
    except Exception as e:
        log.error("admin_reload_config_load_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"failed to load config: {e}")

    changed: list[str] = []
    if _rate_limiter is not None:
        old = _rate_limiter._limits  # noqa: SLF001
        new = new_config.limits
        # Compare by dataclass dict so we get a per-field diff.
        from dataclasses import asdict
        old_d = asdict(old)
        new_d = asdict(new)
        for k, v in new_d.items():
            if old_d.get(k) != v:
                changed.append(f"limits.{k}: {old_d.get(k)!r} → {v!r}")
        _rate_limiter._limits = new  # noqa: SLF001 — single-line atomic ref swap

    log.warning("admin_reload_config", changed=changed or ["(no diffs in hot-reloadable sections)"])

    return {
        "ok": True,
        "changed": changed,
        "note": (
            "server.timeouts / server.host / server.port / database.path / "
            "extension.* require a full restart; the diff above only lists "
            "fields that actually take effect now."
        ),
    }


@router.post("/admin/devices/{device_id}/paused")
async def admin_set_device_paused(
    device_id: str,
    paused: bool,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Deprecated: CDP relay removed in v0.8.0."""
    _check_admin(x_admin_token)
    raise HTTPException(
        status_code=410, detail="CDP relay removed in v0.8.0; use extension popup to pause"
    )


@router.post("/admin/devices/{device_id}/keepalive")
async def admin_set_device_keepalive(
    device_id: str,
    enabled: bool,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Deprecated: CDP relay removed in v0.8.0."""
    _check_admin(x_admin_token)
    raise HTTPException(status_code=410, detail="CDP relay removed in v0.8.0")


@router.post("/admin/devices/{device_id}/force-detach")
async def admin_force_detach(
    device_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Deprecated: CDP relay removed in v0.8.0."""
    _check_admin(x_admin_token)
    raise HTTPException(status_code=410, detail="CDP relay removed in v0.8.0")


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard read endpoints
# ──────────────────────────────────────────────────────────────────────────────


def _online_device_set() -> dict[str, bool]:
    """Returns {device_id: paused} for all currently connected devices."""
    if _connection_manager is None:
        return {}
    result = {}
    for did in _connection_manager.connected_device_ids:
        conn = _connection_manager.get(did)
        result[did] = conn.paused if conn else False
    return result


@router.get("/admin/overview")
async def admin_overview(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _check_admin(x_admin_token)
    assert _db is not None
    online_set = _online_device_set()

    users_total = (await _db.fetchone("SELECT COUNT(*) AS n FROM users"))["n"]
    users_active = (await _db.fetchone("SELECT COUNT(*) AS n FROM users WHERE is_active = 1"))["n"]
    devices_total = (await _db.fetchone("SELECT COUNT(*) AS n FROM devices"))["n"]
    devices_active = (
        await _db.fetchone("SELECT COUNT(*) AS n FROM devices WHERE is_active = 1")
    )["n"]
    sessions_active = (await _db.fetchone(
        "SELECT COUNT(*) AS n FROM sessions WHERE state IN ('created','active')"
    ))["n"]
    cmds_today = (await _db.fetchone(
        "SELECT COUNT(*) AS n FROM commands WHERE created_at >= datetime('now','-1 day')"
    ))["n"]
    cmds_failed = (await _db.fetchone(
        "SELECT COUNT(*) AS n FROM commands "
        "WHERE created_at >= datetime('now','-1 day') AND status IN ('failed','timeout')"
    ))["n"]
    codes_active = (await _db.fetchone(
        "SELECT COUNT(*) AS n FROM pairing_codes WHERE used = 0 AND expires_at > datetime('now')"
    ))["n"]

    limiter_snap = _rate_limiter.snapshot() if _rate_limiter else {}

    return {
        "users": {"total": users_total, "active": users_active},
        "devices": {
            "total": devices_total, "active": devices_active,
            "online": len(online_set),
            "paused": sum(1 for p in online_set.values() if p),
        },
        "sessions": {"active": sessions_active},
        "commands_24h": {"total": cmds_today, "failed": cmds_failed},
        "pairing_codes_active": codes_active,
        "limiter": limiter_snap,
        "uptime_seconds": round(time.time() - _start_time, 1),
        "version": _config.server.version if _config else "unknown",
    }


@router.get("/admin/users")
async def admin_list_users(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> list[dict[str, Any]]:
    _check_admin(x_admin_token)
    assert _db is not None
    online_set = _online_device_set()
    rows = await _db.fetchall(
        "SELECT u.id, u.name, u.is_active, u.evaluate_enabled, u.evaluate_domains, "
        "  u.network_enabled, u.created_at, u.updated_at, "
        "  (SELECT COUNT(*) FROM devices d "
        "WHERE d.user_id = u.id AND d.is_active = 1) AS active_devices, "
        "  (SELECT COUNT(*) FROM devices d WHERE d.user_id = u.id) AS total_devices "
        "FROM users u ORDER BY u.created_at DESC"
    )
    out = []
    for r in rows:
        d = dict(r)
        # Count online devices for this user
        user_devices = await _db.fetchall(
            "SELECT id FROM devices WHERE user_id = ?", (d["id"],)
        )
        d["online_devices"] = sum(1 for dev in user_devices if dev["id"] in online_set)
        out.append(d)
    return out


@router.get("/admin/users/{user_id}")
async def admin_get_user(
    user_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _check_admin(x_admin_token)
    assert _db is not None
    online_set = _online_device_set()

    user = await _db.fetchone(
        "SELECT id, name, is_active, evaluate_enabled, evaluate_domains, "
        "network_enabled, created_at, updated_at "
        "FROM users WHERE id = ?", (user_id,)
    )
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")

    devices = await _db.fetchall(
        "SELECT id, name, is_active, last_seen_at, meta_json, created_at "
        "FROM devices WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    device_list = []
    for dev in devices:
        dd = dict(dev)
        meta_raw = dd.pop("meta_json", "") or "{}"
        try:
            dd["meta"] = json.loads(meta_raw)
        except Exception:
            dd["meta"] = {}
        dd["online"] = dd["id"] in online_set
        dd["paused"] = online_set.get(dd["id"], False)
        device_list.append(dd)

    sessions = await _db.fetchall(
        "SELECT id, device_id, state, created_at, last_activity_at "
        "FROM sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
        (user_id,),
    )
    codes = await _db.fetchall(
        "SELECT id, expires_at, used FROM pairing_codes "
        "WHERE user_id = ? AND used = 0 AND expires_at > datetime('now')",
        (user_id,),
    )
    return {
        "user": dict(user),
        "devices": device_list,
        "recent_sessions": [dict(s) for s in sessions],
        "active_pairing_codes": [dict(c) for c in codes],
    }


@router.get("/admin/devices")
async def admin_list_devices(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> list[dict[str, Any]]:
    _check_admin(x_admin_token)
    assert _db is not None
    online_set = _online_device_set()
    rows = await _db.fetchall(
        "SELECT d.id, d.name, d.is_active, d.last_seen_at, d.meta_json, d.created_at, "
        "  u.name AS user_name, u.id AS user_id "
        "FROM devices d JOIN users u ON u.id = d.user_id ORDER BY d.last_seen_at DESC NULLS LAST"
    )
    out = []
    for r in rows:
        d = dict(r)
        meta_raw = d.pop("meta_json", "") or "{}"
        try:
            d["meta"] = json.loads(meta_raw)
        except Exception:
            d["meta"] = {}
        d["ext_version"] = d["meta"].get("ext_version")
        d["online"] = d["id"] in online_set
        d["paused"] = online_set.get(d["id"], False)
        out.append(d)
    return out


@router.get("/admin/sessions")
async def admin_list_sessions(
    state: str | None = Query(default=None),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> list[dict[str, Any]]:
    _check_admin(x_admin_token)
    assert _db is not None
    sql = (
        "SELECT s.id, s.user_id, s.device_id, s.state, s.created_at, s.last_activity_at, "
        "  u.name AS user_name "
        "FROM sessions s JOIN users u ON u.id = s.user_id"
    )
    params: tuple = ()
    if state:
        sql += " WHERE s.state = ?"
        params = (state,)
    sql += " ORDER BY s.created_at DESC LIMIT 100"
    rows = await _db.fetchall(sql, params)
    return [dict(r) for r in rows]


@router.get("/admin/limiter")
async def admin_limiter(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _check_admin(x_admin_token)
    return _rate_limiter.snapshot() if _rate_limiter else {}


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard write endpoints
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/admin/users")
async def admin_create_user(
    body: dict[str, Any] = Body(...),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _check_admin(x_admin_token)
    assert _db is not None
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    from server.audit.logger import AuditLogger
    audit = AuditLogger(_db)
    return await create_user(_db, name, audit)


@router.post("/admin/users/{user_id}/pairing-code")
async def admin_create_pairing_code(
    user_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _check_admin(x_admin_token)
    assert _db is not None and _config is not None
    user = await _db.fetchone("SELECT id FROM users WHERE id = ? AND is_active = 1", (user_id,))
    if user is None:
        raise HTTPException(status_code=404, detail="user not found or inactive")
    from server.audit.logger import AuditLogger
    audit = AuditLogger(_db)
    return await create_pairing_code(_db, _config, user_id, audit)


@router.post("/admin/users/{user_id}/evaluate")
async def admin_set_evaluate(
    user_id: str,
    body: dict[str, Any] = Body(...),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _check_admin(x_admin_token)
    assert _db is not None
    user = await _db.fetchone("SELECT id FROM users WHERE id = ?", (user_id,))
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    from server.audit.logger import AuditLogger
    audit = AuditLogger(_db)
    if body.get("enabled", True):
        domains = str(body.get("domains", "*"))
        return await grant_evaluate(_db, user_id, domains, audit)
    return await revoke_evaluate(_db, user_id, audit)


@router.post("/admin/users/{user_id}/network")
async def admin_set_network(
    user_id: str,
    body: dict[str, Any] = Body(...),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _check_admin(x_admin_token)
    assert _db is not None
    user = await _db.fetchone("SELECT id FROM users WHERE id = ?", (user_id,))
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    from server.audit.logger import AuditLogger
    audit = AuditLogger(_db)
    if body.get("enabled", True):
        return await grant_network(_db, user_id, audit)
    return await revoke_network(_db, user_id, audit)


@router.post("/admin/users/{user_id}/revoke")
async def admin_revoke_user(
    user_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _check_admin(x_admin_token)
    assert _db is not None
    user = await _db.fetchone("SELECT id, name FROM users WHERE id = ?", (user_id,))
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    from server.audit.logger import AuditLogger
    audit = AuditLogger(_db)
    result = await revoke_user(_db, user_id, audit)
    # Also kick all their devices from WS
    if _connection_manager:
        devices = await _db.fetchall("SELECT id FROM devices WHERE user_id = ?", (user_id,))
        for dev in devices:
            await _connection_manager.revoke(dev["id"])
    return result


@router.post("/admin/devices/{device_id}/revoke")
async def admin_revoke_device(
    device_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _check_admin(x_admin_token)
    assert _db is not None
    dev = await _db.fetchone("SELECT id FROM devices WHERE id = ?", (device_id,))
    if dev is None:
        raise HTTPException(status_code=404, detail="device not found")
    from server.audit.logger import AuditLogger
    audit = AuditLogger(_db)
    result = await revoke_device(_db, device_id, audit)
    if _connection_manager:
        await _connection_manager.revoke(device_id)
    return result
