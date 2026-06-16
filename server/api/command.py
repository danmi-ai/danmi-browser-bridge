"""Command API endpoint — POST /api/v1/command."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from server.auth.dependencies import require_user_auth
from server.auth.evaluate import EvaluateNotAllowed, assert_evaluate_allowed
from server.auth.network import NetworkNotAllowed, assert_network_allowed
from server.auth.validator import AuthInfo
from server.limiter import RateLimitedError
from server.logging import get_logger
from server.storage.database import Database
from server.ws.connection_manager import CommandError, ConnectionManager, DeviceOfflineError

log = get_logger("server.api.command")

router = APIRouter()

_db: Database | None = None
_connection_manager: ConnectionManager | None = None
_rate_limiter = None

_ACTION_TIMEOUTS: dict[str, float] = {
    "navigate": 30.0,
    "snapshot": 15.0,
    "screenshot": 10.0,
    "click": 10.0,
    "fill": 10.0,
    "evaluate": 30.0,
    "network": 10.0,
    "upload": 30.0,
    "save_as_pdf": 30.0,
}
_DEFAULT_TIMEOUT: float = 15.0

_FORBIDDEN_SCHEMES = frozenset(
    ("javascript", "data", "file", "chrome", "chrome-extension", "about")
)


def init_command_router(
    db: Database, connection_manager: ConnectionManager, rate_limiter
) -> APIRouter:
    global _db, _connection_manager, _rate_limiter
    _db = db
    _connection_manager = connection_manager
    _rate_limiter = rate_limiter
    return router


class CommandRequest(BaseModel):
    action: str
    args: dict = {}
    session: str = "default"
    device_id: str | None = None


def _get_online_devices_for_user(user_id: str) -> list[str]:
    """Return device_ids currently connected via WS for the given user."""
    assert _connection_manager is not None
    result: list[str] = []
    for device_id in _connection_manager.connected_device_ids:
        conn = _connection_manager.get(device_id)
        if conn and conn.user_id == user_id:
            result.append(device_id)
    return result


@router.post("/command")
async def execute_command(
    req: CommandRequest,
    request: Request,
    auth: AuthInfo = Depends(require_user_auth),
):
    assert _connection_manager is not None
    assert _db is not None
    assert _rate_limiter is not None

    # --- Resolve device ---
    device_id = req.device_id
    if device_id is None:
        online = _get_online_devices_for_user(auth.user_id)
        if len(online) == 0:
            raise HTTPException(
                status_code=502,
                detail={"error": "No device connected", "code": "DEVICE_OFFLINE"},
            )
        if len(online) > 1:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "Multiple devices online; specify device_id",
                    "code": "AMBIGUOUS_DEVICE",
                    "available": online,
                },
            )
        device_id = online[0]
    else:
        if not _connection_manager.is_connected(device_id):
            raise HTTPException(
                status_code=502,
                detail={"error": "Device not connected", "code": "DEVICE_OFFLINE"},
            )

    # --- Pause gate ---
    # Fast pre-send rejection when the device is paused, so we don't burn a
    # round-trip + rate-limit slot on a command the extension will refuse
    # anyway. The extension enforces this authoritatively; this is the visible,
    # auditable front door. 423 Locked distinguishes "paused" from 502 (offline)
    # and 429 (rate limited).
    paused_conn = _connection_manager.get(device_id)
    if paused_conn is not None and paused_conn.paused:
        raise HTTPException(
            status_code=423,
            detail={"error": "Device is paused by user", "code": "DEVICE_PAUSED"},
        )

    # --- Rate limiting ---
    try:
        _rate_limiter.check_and_acquire(
            session_id=req.session,
            device_id=device_id,
            user_id=auth.user_id,
        )
    except RateLimitedError as e:
        raise HTTPException(
            status_code=429,
            detail={"error": e.message, "code": "RATE_LIMITED"},
            headers={"Retry-After": str(int(e.retry_after_s))},
        )

    try:
        # --- Security gates ---
        if req.action == "evaluate":
            # TODO: We don't know the current page URL at this point;
            # domain allowlist check is deferred to the extension side.
            # For now just check that the user has evaluate enabled.
            try:
                await assert_evaluate_allowed(_db, auth.user_id, None)
            except EvaluateNotAllowed as e:
                raise HTTPException(
                    status_code=403,
                    detail={"error": e.reason, "code": "EVALUATE_NOT_ALLOWED"},
                )

        if req.action == "navigate":
            url = req.args.get("url", "")
            try:
                parsed = urlparse(url)
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "Invalid URL", "code": "INVALID_URL"},
                )
            scheme = (parsed.scheme or "").lower()
            if scheme in _FORBIDDEN_SCHEMES:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": f"URL scheme '{scheme}' is not allowed",
                        "code": "SCHEME_NOT_ALLOWED",
                    },
                )

        if req.action == "network":
            try:
                await assert_network_allowed(_db, auth.user_id)
            except NetworkNotAllowed as e:
                raise HTTPException(
                    status_code=403,
                    detail={"error": e.reason, "code": "NETWORK_NOT_ALLOWED"},
                )

        if req.action == "upload":
            files = req.args.get("files", [])
            total_size = sum(len(f.get("data", "")) for f in files if isinstance(f, dict))
            if total_size > 5 * 1024 * 1024:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "Upload exceeds 5MB limit", "code": "UPLOAD_TOO_LARGE"},
                )

        # --- Build and send command ---
        msg = {
            "type": "command",
            "cmd": req.action,
            "params": req.args,
            "session": req.session,
        }

        timeout = _ACTION_TIMEOUTS.get(req.action, _DEFAULT_TIMEOUT)

        result = await _connection_manager.send_and_wait(device_id, msg, timeout)
        return {"result": result}

    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail={"error": "Command timed out", "code": "TIMEOUT"},
        )
    except (DeviceOfflineError, CommandError) as e:
        if isinstance(e, CommandError):
            raise HTTPException(
                status_code=500,
                detail={
                    "error": e.response.get("message", str(e)),
                    "code": e.response.get("code", "COMMAND_ERROR"),
                },
            )
        raise HTTPException(
            status_code=502,
            detail={"error": "Device not connected", "code": "DEVICE_OFFLINE"},
        )
    except Exception as e:
        log.error("command_unexpected_error", error=str(e), action=req.action)
        raise HTTPException(
            status_code=500,
            detail={"error": "Internal server error", "code": "INTERNAL_ERROR"},
        )
    finally:
        _rate_limiter.release(device_id=device_id)
