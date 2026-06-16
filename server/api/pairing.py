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

    # PROV-7: every failure path returns ONE generic error so the response can't
    # be used as an enumeration oracle (invalid vs. used vs. expired). The
    # specific reason is still recorded server-side in the audit log.
    generic_error = HTTPException(
        status_code=400, detail="Invalid or expired pairing code"
    )

    # LOG-1: codes are stored hashed; look up by the hash of the submitted code.
    row = await _db.fetchone(
        "SELECT id, user_id, expires_at, used FROM pairing_codes WHERE code_hash = ?",
        (hash_token(req.pairing_code),),
    )

    if row is None:
        if _audit:
            await _audit.log(
                "pair_failed",
                detail={"reason": "invalid_code", "device_name": req.device_name},
            )
        raise generic_error

    if row["used"]:
        if _audit:
            await _audit.log(
                "pair_failed",
                actor_id=row["user_id"],
                detail={"reason": "already_used", "device_name": req.device_name},
            )
        raise generic_error

    expires_at = datetime.fromisoformat(row["expires_at"]).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if now > expires_at:
        if _audit:
            await _audit.log(
                "pair_failed",
                actor_id=row["user_id"],
                detail={"reason": "expired", "device_name": req.device_name},
            )
        raise generic_error

    device_id = str(uuid.uuid4())
    device_token = generate_device_token()
    token_hash = hash_token(device_token)
    user_id = row["user_id"]
    pairing_id = row["id"]

    # PROV-1: claim the code atomically. The conditional UPDATE only succeeds for
    # the request that flips used 0->1; a concurrent redeemer sees rowcount 0 and
    # loses the race. Claim + device INSERT share one transaction so a failed
    # claim never leaves a dangling device.
    async with _db.transaction() as conn:
        cursor = await conn.execute(
            "UPDATE pairing_codes SET used = 1, used_by_device_id = ? WHERE id = ? AND used = 0",
            (device_id, pairing_id),
        )
        if cursor.rowcount != 1:
            # Lost the race (already claimed between the read above and now).
            if _audit:
                await _audit.log(
                    "pair_failed",
                    actor_id=user_id,
                    detail={"reason": "already_used", "device_name": req.device_name},
                )
            raise generic_error
        await conn.execute(
            "INSERT INTO devices (id, user_id, name, token_hash) VALUES (?, ?, ?, ?)",
            (device_id, user_id, req.device_name, token_hash),
        )

    if _audit:
        await _audit.log(
            "pair_success",
            actor_id=user_id,
            detail={"device_id": device_id, "device_name": req.device_name},
        )

    return PairResponse(device_token=device_token, device_id=device_id)
