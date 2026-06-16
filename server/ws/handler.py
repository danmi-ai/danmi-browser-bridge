"""WebSocket endpoint handler for device connections."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from server.audit.logger import AuditLogger
from server.auth.validator import TokenValidator
from server.config import AppConfig
from server.logging import get_logger
from server.storage.database import Database
from server.utils.version import lt as version_lt
from server.ws.connection_manager import ConnectionManager

log = get_logger("server.ws")

_manager: ConnectionManager | None = None
_validator: TokenValidator | None = None
_config: AppConfig | None = None
_audit: AuditLogger | None = None
_db: Database | None = None


def init_ws_router(
    db: Database,
    config: AppConfig,
    manager: ConnectionManager,
    audit_logger: AuditLogger | None = None,
) -> APIRouter:
    """Initialize WebSocket router with dependencies."""
    global _manager, _validator, _config, _audit, _db
    _manager = manager
    _validator = TokenValidator(db)
    _config = config
    _audit = audit_logger
    _db = db

    return router


router = APIRouter()


@router.websocket("/ws/device")
async def device_ws(websocket: WebSocket, token: str | None = Query(default=None)):
    """WebSocket endpoint for device connections.

    Two auth modes (CODE_REVIEW C-2):
    1. **legacy**: token in `?token=` query string. Logged to access logs;
       still accepted for backwards-compat with already-deployed extensions.
    2. **post-handshake** (preferred): no query token; the first frame must be
       `{"type":"auth","token":"bb_dev_..."}`. The token never appears in URLs.
    """
    AUTH_TIMEOUT_S = 10

    await websocket.accept()

    # Optional auth-frame meta (#16): forwarded by the extension on connect.
    auth_meta: dict | None = None

    # If query token provided -> legacy path.
    if token is None:
        # Wait for explicit auth frame.
        try:
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=AUTH_TIMEOUT_S
            )
        except (asyncio.TimeoutError, WebSocketDisconnect):
            log.warning("ws_auth_timeout")
            try:
                await websocket.close(code=4001, reason="auth_timeout")
            except Exception:
                pass
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("ws_auth_invalid_json")
            await websocket.close(code=4001, reason="auth_invalid")
            return
        if msg.get("type") != "auth" or not isinstance(msg.get("token"), str):
            log.warning("ws_auth_missing")
            await websocket.close(code=4001, reason="auth_required")
            return
        token = msg["token"]
        if isinstance(msg.get("meta"), dict):
            auth_meta = msg["meta"]

    auth = await _validator.validate(token)
    if auth is None or auth.token_type != "device":
        log.warning("ws_auth_failed", token_prefix=token[:10])
        if _audit:
            await _audit.log(
                "token_validation_failed",
                detail={"context": "ws_device", "token_prefix": token[:10]},
            )
        try:
            await websocket.close(code=4001, reason="auth_failed")
        except Exception:
            pass
        return

    # Enforce minimum extension version (#16). Devices using legacy ?token=
    # path won't have meta — they're old clients by definition; we still
    # accept them so existing pairings keep working until the user upgrades,
    # *unless* min_version explicitly demands a version we know they're below.
    ext_version = (auth_meta or {}).get("ext_version")
    min_version = _config.extension.min_version if _config else ""
    if ext_version and min_version and version_lt(str(ext_version), min_version):
        log.warning(
            "ws_extension_too_old",
            device_id=auth.id,
            ext_version=ext_version,
            min_version=min_version,
        )
        if _audit:
            await _audit.log(
                "extension_rejected_too_old",
                actor_id=auth.user_id,
                detail={
                    "device_id": auth.id,
                    "ext_version": ext_version,
                    "min_version": min_version,
                },
            )
        try:
            # Tell the client why before closing so the popup can surface it.
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "force_upgrade",
                        "reason": "extension_too_old",
                        "min_version": min_version,
                        "ext_version": ext_version,
                    }
                )
            )
            await websocket.close(code=4002, reason="extension_too_old")
        except Exception:
            pass
        return

    # Persist meta on the device row so admins / CLI can see what's connecting.
    if auth_meta is not None and _db is not None:
        try:
            await _db.execute(
                "UPDATE devices SET meta_json = ?, updated_at = ?, last_seen_at = ? WHERE id = ?",
                (
                    json.dumps(auth_meta, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    auth.id,
                ),
            )
        except Exception as e:
            log.warning("ws_meta_persist_failed", device_id=auth.id, error=str(e))

    # Confirm auth back to client (post-handshake mode); harmless for legacy.
    try:
        await websocket.send_text(json.dumps({"type": "auth_ok", "device_id": auth.id}))
    except Exception:
        pass

    conn = await _manager.connect(
        device_id=auth.id,
        user_id=auth.user_id,
        device_name=auth.name,
        websocket=websocket,
    )

    log.info("device_connected", device_id=auth.id, device_name=auth.name)

    heartbeat_interval = _config.server.timeouts.ws_heartbeat_interval
    heartbeat_timeout = _config.server.timeouts.ws_heartbeat_timeout
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(websocket, conn.device_id, heartbeat_interval, heartbeat_timeout)
    )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("ws_invalid_json", device_id=conn.device_id)
                continue

            msg_type = msg.get("type")

            # Route command responses to pending futures first. Pass the
            # authenticated socket's own device_id so a device can only resolve
            # commands it actually owns (no cross-device result-forgery).
            if msg_type in ("result", "error") and "msg_id" in msg:
                _manager.resolve_command(conn.device_id, msg["msg_id"], msg)
                continue

            if msg_type == "pong":
                _manager.update_heartbeat(conn.device_id)
                continue

            if msg_type == "extension.paused_changed":
                paused = bool(msg.get("paused"))
                _manager.set_paused(conn.device_id, paused)
                log.info("device_paused_changed", device_id=conn.device_id, paused=paused)
                if _audit:
                    await _audit.log(
                        "device_paused_changed",
                        actor_id=conn.user_id,
                        detail={"device_id": conn.device_id, "paused": paused},
                    )
                continue
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws_unexpected_error", device_id=conn.device_id, error=str(e))
    finally:
        heartbeat_task.cancel()
        await _manager.disconnect(conn.device_id, conn)
        log.info("device_disconnected", device_id=conn.device_id)


async def _heartbeat_loop(
    websocket: WebSocket, device_id: str, interval: int, timeout: int
) -> None:
    """Send heartbeat pings and disconnect stale devices."""
    try:
        while True:
            await asyncio.sleep(interval)

            if websocket.client_state != WebSocketState.CONNECTED:
                break

            last_hb = _manager.get(device_id)
            if last_hb and (time.time() - last_hb.last_heartbeat) > (interval + timeout):
                log.warning("heartbeat_timeout", device_id=device_id)
                await websocket.close(code=4004, reason="heartbeat_timeout")
                break

            ping_msg = json.dumps({"type": "ping", "ts": time.time()})
            await websocket.send_text(ping_msg)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.debug("heartbeat_loop_error", device_id=device_id, error=str(e))
