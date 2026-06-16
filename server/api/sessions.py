"""Session REST endpoints — create, get, close sessions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.auth.dependencies import require_user_auth
from server.auth.validator import AuthInfo
from server.logging import get_logger
from server.sessions.manager import SessionError, SessionManager

log = get_logger("server.sessions")

router = APIRouter()

_session_manager: SessionManager | None = None


def init_sessions_router(session_manager: SessionManager) -> APIRouter:
    global _session_manager
    _session_manager = session_manager
    return router


class CreateSessionRequest(BaseModel):
    device_id: str


class SessionResponse(BaseModel):
    id: str
    user_id: str
    device_id: str
    state: str
    created_at: str
    activated_at: str | None = None
    closed_at: str | None = None
    last_activity_at: str
    close_reason: str | None = None


@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def create_session(req: CreateSessionRequest, auth: AuthInfo = Depends(require_user_auth)):
    assert _session_manager is not None
    try:
        session = await _session_manager.create_session(auth.user_id, req.device_id)
    except SessionError as e:
        status = 400
        if e.code == "DEVICE_OFFLINE":
            status = 409
        elif e.code == "DEVICE_NOT_FOUND":
            status = 404
        hint = {
            "DEVICE_OFFLINE": (
                "the paired browser is not connected; "
                "ask the user to open Chrome / re-pair"
            ),
            "DEVICE_NOT_FOUND": (
                "unknown device_id; call GET /api/v1/devices to list paired devices"
            ),
        }.get(e.code, "check the device_id and the device's pairing status")
        raise HTTPException(
            status_code=status,
            detail={
                "code": e.code,
                "message": e.message,
                "details": {
                    "device_id": req.device_id,
                    "step": "create_session",
                    "hint": hint,
                },
            },
        )
    log.info("session_created", session_id=session["id"], device_id=req.device_id)
    return SessionResponse(**session)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, auth: AuthInfo = Depends(require_user_auth)):
    assert _session_manager is not None
    session = await _session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SESSION_NOT_FOUND",
                "message": "Session not found",
                "details": {"session_id": session_id, "step": "get_session"},
            },
        )
    if session["user_id"] != auth.user_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN",
                "message": "Not your session",
                "details": {"session_id": session_id, "step": "authorize_session"},
            },
        )
    return SessionResponse(**session)


@router.delete("/sessions/{session_id}", response_model=SessionResponse)
async def close_session(session_id: str, auth: AuthInfo = Depends(require_user_auth)):
    assert _session_manager is not None
    session = await _session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SESSION_NOT_FOUND",
                "message": "Session not found",
                "details": {"session_id": session_id, "step": "close_session"},
            },
        )
    if session["user_id"] != auth.user_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN",
                "message": "Not your session",
                "details": {"session_id": session_id, "step": "authorize_session"},
            },
        )
    result = await _session_manager.close_session(session_id, reason="user_closed")
    log.info("session_closed", session_id=session_id, reason="user_closed")
    return SessionResponse(**result)
