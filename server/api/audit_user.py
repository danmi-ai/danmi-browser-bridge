"""User-facing audit log query (#13).

A user can call ``GET /api/v1/audit/me`` with their own ``Bearer`` user_token
to enumerate audit entries that name them as the actor. They can also flag a
specific entry as "this wasn't me" via ``POST /api/v1/audit/{id}/dispute``,
which appends a ``dispute_raised`` row to the audit chain.

The existing admin endpoint ``GET /api/v1/audit`` (in ``server/api/audit.py``)
returns *all* entries; this module is the user-scoped counterpart.

v0.5.0: OAuth-like short-lived **audit sessions** so the audit web page
(``web/audit/index.html``) never receives the long-lived ``user_token`` in its
URL. Flow:

  1. popup → ``POST /api/v1/audit/session`` with Bearer user_token
     → returns ``{audit_session_id, expires_at}`` (5 min TTL, single user_id)
  2. popup opens ``web/audit/index.html?sid=<audit_session_id>`` in a new tab
  3. page calls ``POST /api/v1/audit/query`` with ``{sid, ...filters}``
     → returns the same shape as ``GET /audit/me``

The legacy ``/audit/me`` endpoint stays for backward compat (tools that already
grew a Bearer-token habit), but the popup flow uses sid.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from server.audit.logger import AuditLogger
from server.auth.dependencies import require_any_auth, require_user_auth
from server.auth.validator import AuthInfo
from server.logging import get_logger
from server.storage.database import Database

log = get_logger("server.audit_user")

router = APIRouter()

_db: Database | None = None
_audit: AuditLogger | None = None

# Audit-session TTL: 5 minutes. Long enough for the user to open a tab and
# poke around their history, short enough that a leaked sid is mostly harmless.
AUDIT_SESSION_TTL_SECONDS = 300


def init_audit_user_router(db: Database, audit_logger: AuditLogger) -> APIRouter:
    global _db, _audit
    _db = db
    _audit = audit_logger
    return router


def _parse_iso(s: str) -> datetime | None:
    try:
        # Accept "2026-05-28T08:00:00Z" / "2026-05-28T08:00:00+00:00"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


async def _query_user_audit(
    user_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
    cmd: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Shared implementation behind ``GET /audit/me`` and ``POST /audit/query``."""
    assert _db is not None
    sql = (
        "SELECT id, event_type, actor_id, session_id, detail, created_at "
        "FROM audit_log WHERE actor_id = ?"
    )
    params: list[Any] = [user_id]

    if since:
        ts = _parse_iso(since)
        if ts is None:
            raise HTTPException(status_code=400, detail="invalid since timestamp")
        sql += " AND created_at >= ?"
        params.append(ts.astimezone(timezone.utc).isoformat())
    if until:
        tu = _parse_iso(until)
        if tu is None:
            raise HTTPException(status_code=400, detail="invalid until timestamp")
        sql += " AND created_at <= ?"
        params.append(tu.astimezone(timezone.utc).isoformat())
    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type)

    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = await _db.fetchall(sql, tuple(params))
    items: list[dict[str, Any]] = []
    import json
    for r in rows:
        detail = {}
        try:
            detail = json.loads(r["detail"]) if r["detail"] else {}
        except Exception:
            detail = {"_raw": r["detail"]}
        if cmd and detail.get("cmd") != cmd:
            continue
        items.append(
            {
                "id": r["id"],
                "event_type": r["event_type"],
                "actor_id": r["actor_id"],
                "session_id": r["session_id"],
                "cmd": detail.get("cmd"),
                "target_url": detail.get("target_url"),
                "status": detail.get("status"),
                "duration_ms": detail.get("duration_ms"),
                "detail": detail,
                "created_at": r["created_at"],
            }
        )
    return {"items": items, "limit": limit, "offset": offset, "count": len(items)}


async def _resolve_audit_session(sid: str) -> str:
    """Validate sid → return user_id, or raise 401/410."""
    assert _db is not None
    row = await _db.fetchone(
        "SELECT user_id, expires_at, revoked_at FROM audit_sessions WHERE id = ?",
        (sid,),
    )
    if row is None:
        raise HTTPException(status_code=401, detail="invalid audit session")
    if row["revoked_at"] is not None:
        raise HTTPException(status_code=410, detail="audit session revoked")
    expires = _parse_iso(row["expires_at"])
    if expires is None or expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="audit session expired")
    return row["user_id"]


@router.post("/audit/session")
async def create_audit_session(
    auth: AuthInfo = Depends(require_user_auth),
) -> dict[str, Any]:
    """Mint a short-lived audit session id bound to the caller.

    Replaces the old "paste your user_token into the audit page URL" flow.
    Popup hits this endpoint with the long-lived user_token, then opens
    ``web/audit/index.html?sid=<id>`` in a new tab. The page never sees the
    user_token.
    """
    assert _db is not None
    sid = "bb_audit_" + secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=AUDIT_SESSION_TTL_SECONDS)
    await _db.execute(
        "INSERT INTO audit_sessions (id, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (sid, auth.user_id, expires.isoformat(), now.isoformat()),
    )
    log.info("audit_session_created", user_id=auth.user_id, sid_prefix=sid[:14])
    return {
        "audit_session_id": sid,
        "expires_at": expires.isoformat(),
        "ttl_seconds": AUDIT_SESSION_TTL_SECONDS,
    }


@router.post("/audit/query")
async def query_audit_with_session(
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Query audit log using an audit_session_id (no Bearer token).

    Body shape::

        {
          "sid": "bb_audit_...",
          "since": "...", "until": "...",
          "cmd": "...", "event_type": "...",
          "limit": 50, "offset": 0
        }
    """
    sid = str(body.get("sid") or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="missing sid")
    user_id = await _resolve_audit_session(sid)

    limit = int(body.get("limit") or 50)
    offset = int(body.get("offset") or 0)
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit out of range")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")

    return await _query_user_audit(
        user_id,
        since=body.get("since"),
        until=body.get("until"),
        cmd=body.get("cmd"),
        event_type=body.get("event_type"),
        limit=limit,
        offset=offset,
    )


@router.get("/audit/me")
async def get_my_audit(
    auth: AuthInfo = Depends(require_user_auth),
    since: str | None = Query(default=None, description="ISO 8601 lower bound"),
    until: str | None = Query(default=None, description="ISO 8601 upper bound"),
    cmd: str | None = Query(default=None, description="filter detail.cmd"),
    event_type: str | None = Query(default=None, description="filter event_type"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Return audit rows where ``actor_id == self`` (legacy Bearer flow)."""
    return await _query_user_audit(
        auth.user_id,
        since=since,
        until=until,
        cmd=cmd,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )


@router.get("/audit/stats")
async def get_audit_stats(
    auth: AuthInfo = Depends(require_any_auth),
    window: int = Query(
        default=86400, ge=60, le=2592000, description="Time window in seconds (default 24h)"
    ),
) -> dict[str, Any]:
    """Return high-sensitivity command counts for the authenticated user.

    Used by the popup Settings tab to show 'evaluate N times / fetch_with_cookies M times'
    in the last `window` seconds (default 24h).

    Returns::

        {
          "window_seconds": 86400,
          "command_counts": {"evaluate": 12, "fetch_with_cookies": 3}
        }
    """
    import json as _json
    assert _db is not None
    from datetime import datetime, timedelta, timezone
    since_dt = datetime.now(timezone.utc) - timedelta(seconds=window)
    rows = await _db.fetchall(
        "SELECT detail FROM audit_log WHERE actor_id = ? "
        "AND event_type = 'command_executed' AND created_at >= ?",
        (auth.user_id, since_dt.isoformat()),
    )
    counts: dict[str, int] = {}
    for r in rows:
        try:
            detail = _json.loads(r["detail"]) if r["detail"] else {}
        except Exception:
            continue
        cmd = detail.get("cmd")
        if cmd:
            counts[cmd] = counts.get(cmd, 0) + 1
    # Only expose high-sensitivity commands in the public response.
    HIGH_SENS = {"evaluate", "fetch_with_cookies"}
    return {
        "window_seconds": window,
        "command_counts": {k: counts.get(k, 0) for k in HIGH_SENS},
    }


@router.post("/audit/{audit_id}/dispute")
async def dispute_audit_entry(
    audit_id: int,
    note: dict[str, Any] | None = None,
    auth: AuthInfo = Depends(require_user_auth),
) -> dict[str, Any]:
    """Mark a specific audit entry as 'this wasn't me'.

    Appends a ``dispute_raised`` audit row referencing the original. Only
    succeeds if the original row's actor_id matches the caller (you can only
    dispute your own entries).
    """
    assert _db is not None
    assert _audit is not None
    row = await _db.fetchone(
        "SELECT id, event_type, actor_id, session_id, detail, created_at "
        "FROM audit_log WHERE id = ?",
        (audit_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="audit entry not found")
    if row["actor_id"] != auth.user_id:
        raise HTTPException(
            status_code=403, detail="cannot dispute another user's audit entry"
        )

    user_note = ""
    if isinstance(note, dict):
        user_note = str(note.get("note") or "")[:500]

    await _audit.log(
        "dispute_raised",
        actor_id=auth.user_id,
        session_id=row["session_id"],
        detail={
            "disputed_id": audit_id,
            "disputed_event_type": row["event_type"],
            "disputed_created_at": row["created_at"],
            "note": user_note,
        },
    )
    log.warning(
        "audit_disputed",
        user_id=auth.user_id,
        disputed_id=audit_id,
        disputed_event_type=row["event_type"],
    )
    return {"ok": True, "disputed_id": audit_id}
