"""Shared authentication dependency for FastAPI routes."""

from __future__ import annotations

from fastapi import Header, HTTPException

from server.auth.validator import AuthInfo, TokenValidator
from server.storage.database import Database

_validator: TokenValidator | None = None


def init_auth_dependency(db: Database) -> None:
    global _validator
    _validator = TokenValidator(db)


async def require_user_auth(authorization: str = Header(...)) -> AuthInfo:
    """Extract and validate bearer token. Requires user-level token."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]
    auth = await _validator.validate(token)
    if auth is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if auth.token_type != "user":
        raise HTTPException(status_code=403, detail="User token required")
    return auth


async def require_any_auth(authorization: str = Header(...)) -> AuthInfo:
    """Accept either user or device token. Device tokens resolve to the owning user."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]
    auth = await _validator.validate(token)
    if auth is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return auth
