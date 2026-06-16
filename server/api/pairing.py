"""Pairing endpoint — extension pairs with server using a one-time code."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from server.audit.logger import AuditLogger
from server.auth.tokens import generate_device_token, hash_token
from server.logging import get_logger
from server.storage.database import Database

log = get_logger("server.api.pairing")

router = APIRouter()

_db: Database | None = None
_audit: AuditLogger | None = None

# Per-IP sliding-window rate limiter for pair attempts.
# Single-process in-memory; sufficient for the current single-worker deployment.
# CODE_REVIEW C-1.
_RATE_WINDOW_S = 60
_RATE_MAX_PER_WINDOW = 10
_pair_attempts: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(client_ip: str) -> None:
    now = time.monotonic()
    bucket = _pair_attempts[client_ip]
    # purge old entries
    cutoff = now - _RATE_WINDOW_S
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= _RATE_MAX_PER_WINDOW:
        retry_after = int(_RATE_WINDOW_S - (now - bucket[0])) + 1
        log.warning(
            "pair_rate_limited",
            client_ip=client_ip,
            attempts=len(bucket),
            retry_after=retry_after,
        )
        raise HTTPException(
            status_code=429,
            detail="Too many pairing attempts; try again later",
            headers={"Retry-After": str(retry_after)},
        )
    bucket.append(now)


def init_pairing_router(db: Database, audit_logger: AuditLogger | None = None) -> APIRouter:
    global _db, _audit
    _db = db
    _audit = audit_logger
    return router


class PairRequest(BaseModel):
    pairing_code: str
    device_name: str


class PairResponse(BaseModel):
    device_token: str
    device_id: str


@router.post("/pair", response_model=PairResponse)
async def pair_device(req: PairRequest, request: Request) -> PairResponse:
    assert _db is not None

    # Rate-limit by client IP before touching the DB.
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    row = await _db.fetchone(
        "SELECT id, user_id, code, expires_at, used FROM pairing_codes WHERE code = ?",
        (req.pairing_code,),
    )

    if row is None:
        if _audit:
            await _audit.log(
                "pair_failed",
                detail={"reason": "invalid_code", "device_name": req.device_name},
            )
        raise HTTPException(status_code=400, detail="Invalid pairing code")

    if row["used"]:
        if _audit:
            await _audit.log(
                "pair_failed",
                actor_id=row["user_id"],
                detail={"reason": "already_used", "device_name": req.device_name},
            )
        raise HTTPException(status_code=400, detail="Pairing code already used")

    expires_at = datetime.fromisoformat(row["expires_at"]).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if now > expires_at:
        if _audit:
            await _audit.log(
                "pair_failed",
                actor_id=row["user_id"],
                detail={"reason": "expired", "device_name": req.device_name},
            )
        raise HTTPException(status_code=400, detail="Pairing code expired")

    device_id = str(uuid.uuid4())
    device_token = generate_device_token()
    token_hash = hash_token(device_token)
    user_id = row["user_id"]
    pairing_id = row["id"]

    async with _db.transaction() as conn:
        await conn.execute(
            "INSERT INTO devices (id, user_id, name, token_hash) VALUES (?, ?, ?, ?)",
            (device_id, user_id, req.device_name, token_hash),
        )
        await conn.execute(
            "UPDATE pairing_codes SET used = 1, used_by_device_id = ? WHERE id = ?",
            (device_id, pairing_id),
        )

    if _audit:
        await _audit.log(
            "pair_success",
            actor_id=user_id,
            detail={"device_id": device_id, "device_name": req.device_name},
        )

    return PairResponse(device_token=device_token, device_id=device_id)
