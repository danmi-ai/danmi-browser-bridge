"""Onboard endpoint — create user + pairing code in one shot (admin-only)."""

from __future__ import annotations

import hashlib
import hmac
import os
import random
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request

from server.logging import get_logger
from server.storage.database import Database

log = get_logger("server.api.onboard")

router = APIRouter()

_db: Database | None = None
_admin_token_path: Path | None = None


def init_onboard_router(db: Database, *, admin_token_path: Path) -> APIRouter:
    global _db, _admin_token_path
    _db = db
    _admin_token_path = admin_token_path
    return router


def _check_admin(authorization: str | None) -> None:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    # Expect "Bearer <token>"
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0] != "Bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization format")
    token = parts[1]
    if _admin_token_path is None or not _admin_token_path.exists():
        raise HTTPException(
            status_code=503,
            detail="admin token not provisioned",
        )
    expected = _admin_token_path.read_text().strip()
    if not expected:
        raise HTTPException(status_code=503, detail="admin token file is empty")
    a = hashlib.sha256(token.encode()).digest()
    b = hashlib.sha256(expected.encode()).digest()
    if not hmac.compare_digest(a, b):
        raise HTTPException(status_code=403, detail="invalid admin token")


@router.post("/onboard/{username}")
async def onboard_user(
    username: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    assert _db is not None
    _check_admin(authorization)

    # Check if user exists
    row = await _db.fetchone(
        "SELECT id FROM users WHERE name = ?", (username,)
    )

    if row:
        user_id = row["id"]
    else:
        # Create new user
        user_id = str(uuid.uuid4())
        token = "bb_usr_" + secrets.token_hex(16)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        await _db.execute(
            "INSERT INTO users (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, username, token_hash, now),
        )
        log.info("user_created", username=username, user_id=user_id)

    # Generate pairing code
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    expires_at_str = expires_at.isoformat()
    code_id = str(uuid.uuid4())

    await _db.execute(
        "INSERT INTO pairing_codes (id, code, user_id, expires_at, used) VALUES (?, ?, ?, ?, 0)",
        (code_id, code, user_id, expires_at_str),
    )

    log.info("onboard_complete", username=username, user_id=user_id, code=code)

    # Derive server_url from the request (or BB_PUBLIC_URL behind a proxy) so we
    # never hard-code an IP — a relocated deployment keeps working untouched.
    server_url = (os.environ.get("BB_PUBLIC_URL") or str(request.base_url)).rstrip("/")

    return {
        "server_url": server_url,
        "pairing_code": code,
        "expires_at": expires_at_str,
        "user_id": user_id,
    }
