"""Token validation against the database."""

from __future__ import annotations

from dataclasses import dataclass

from server.auth.tokens import hash_token, is_device_token, is_user_token
from server.storage.database import Database


@dataclass
class AuthInfo:
    """Authenticated identity extracted from a valid token."""

    token_type: str  # "user" or "device"
    id: str
    user_id: str
    name: str


class TokenValidator:
    """Validate bearer tokens against stored hashes."""

    def __init__(self, db: Database):
        self._db = db

    async def validate(self, token: str) -> AuthInfo | None:
        """Validate a token and return auth info, or None if invalid."""
        if is_user_token(token):
            return await self._validate_user(token)
        elif is_device_token(token):
            return await self._validate_device(token)
        return None

    async def _validate_user(self, token: str) -> AuthInfo | None:
        token_hash = hash_token(token)
        row = await self._db.fetchone(
            "SELECT id, name FROM users WHERE token_hash = ? AND is_active = 1",
            (token_hash,),
        )
        if row is None:
            return None
        return AuthInfo(
            token_type="user",
            id=row["id"],
            user_id=row["id"],
            name=row["name"],
        )

    async def _validate_device(self, token: str) -> AuthInfo | None:
        token_hash = hash_token(token)
        row = await self._db.fetchone(
            "SELECT id, user_id, name FROM devices WHERE token_hash = ? AND is_active = 1",
            (token_hash,),
        )
        if row is None:
            return None
        return AuthInfo(
            token_type="device",
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
        )
