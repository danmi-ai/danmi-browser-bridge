"""Shared admin write operations — used by both CLI and API."""

from __future__ import annotations

import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

from server.audit.logger import AuditLogger
from server.auth.tokens import generate_user_token, hash_token
from server.config import AppConfig
from server.storage.database import Database


async def create_user(db: Database, name: str, audit: AuditLogger | None = None) -> dict:
    user_id = str(uuid.uuid4())
    token = generate_user_token()
    token_hash = hash_token(token)
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        (user_id, name, token_hash),
    )
    if audit:
        await audit.log("user_created", actor_id="admin", detail={"user_id": user_id, "name": name})
    return {"user_id": user_id, "name": name, "token": token}


async def create_pairing_code(
    db: Database, config: AppConfig, user_id: str, audit: AuditLogger | None = None
) -> dict:
    code_length = config.pairing.code_length
    alphabet = string.ascii_uppercase + string.digits
    code = "".join(secrets.choice(alphabet) for _ in range(code_length))
    expiry_seconds = config.pairing.code_expiry
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expiry_seconds)
    pairing_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO pairing_codes (id, user_id, code, expires_at) VALUES (?, ?, ?, ?)",
        (pairing_id, user_id, code, expires_at.isoformat()),
    )
    if audit:
        await audit.log(
            "pairing_code_created", actor_id="admin", detail={"user_id": user_id, "code": code}
        )
    return {"code": code, "expires_at": expires_at.isoformat(), "ttl_seconds": expiry_seconds}


async def grant_evaluate(
    db: Database, user_id: str, domains: str, audit: AuditLogger | None = None
) -> dict:
    await db.execute(
        "UPDATE users SET evaluate_enabled = 1, evaluate_domains = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (domains, user_id),
    )
    if audit:
        await audit.log(
            "evaluate_granted", actor_id="admin", detail={"user_id": user_id, "domains": domains}
        )
    return {"evaluate_enabled": True, "evaluate_domains": domains}


async def revoke_evaluate(db: Database, user_id: str, audit: AuditLogger | None = None) -> dict:
    await db.execute(
        "UPDATE users SET evaluate_enabled = 0, updated_at = datetime('now') WHERE id = ?",
        (user_id,),
    )
    if audit:
        await audit.log("evaluate_revoked", actor_id="admin", detail={"user_id": user_id})
    return {"evaluate_enabled": False}


async def grant_network(db: Database, user_id: str, audit: AuditLogger | None = None) -> dict:
    await db.execute(
        "UPDATE users SET network_enabled = 1, updated_at = datetime('now') WHERE id = ?",
        (user_id,),
    )
    if audit:
        await audit.log("network_granted", actor_id="admin", detail={"user_id": user_id})
    return {"network_enabled": True}


async def revoke_network(db: Database, user_id: str, audit: AuditLogger | None = None) -> dict:
    await db.execute(
        "UPDATE users SET network_enabled = 0, updated_at = datetime('now') WHERE id = ?",
        (user_id,),
    )
    if audit:
        await audit.log("network_revoked", actor_id="admin", detail={"user_id": user_id})
    return {"network_enabled": False}


async def revoke_user(db: Database, user_id: str, audit: AuditLogger | None = None) -> dict:
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE users SET is_active = 0, updated_at = datetime('now') WHERE id = ?",
            (user_id,),
        )
        await conn.execute(
            "UPDATE devices SET is_active = 0, updated_at = datetime('now') WHERE user_id = ?",
            (user_id,),
        )
    if audit:
        await audit.log("user_revoked", actor_id="admin", detail={"user_id": user_id})
    return {"revoked": True, "user_id": user_id}


async def revoke_device(db: Database, device_id: str, audit: AuditLogger | None = None) -> dict:
    await db.execute(
        "UPDATE devices SET is_active = 0, updated_at = datetime('now') WHERE id = ?",
        (device_id,),
    )
    if audit:
        await audit.log("device_revoked", actor_id="admin", detail={"device_id": device_id})
    return {"revoked": True, "device_id": device_id}
