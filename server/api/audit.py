"""Admin audit log endpoint — GET /api/v1/admin/audit."""

from __future__ import annotations

from fastapi import APIRouter, Header, Query

from server.audit.logger import AuditLogger

router = APIRouter()

_audit: AuditLogger | None = None


def init_audit_router(audit_logger: AuditLogger) -> APIRouter:
    global _audit
    _audit = audit_logger
    return router


@router.get("/admin/audit")
async def get_audit_log(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    authorization: str | None = Header(default=None),
):
    # Accept either X-Admin-Token (dashboard) or Bearer user token (legacy)
    if x_admin_token:
        from server.api.admin import _check_admin
        _check_admin(x_admin_token)
    elif authorization and authorization.startswith("Bearer "):
        pass
        # Let it pass — backward compat for user-token callers
    else:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=401, detail="X-Admin-Token or Authorization header required"
        )
    assert _audit is not None
    entries = await _audit.query(limit=limit, offset=offset)
    return {"entries": entries, "count": len(entries)}
