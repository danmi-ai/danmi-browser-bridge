"""Token generation and hashing for user and device authentication."""

from __future__ import annotations

import hashlib
import secrets

_USER_PREFIX = "bb_usr_"
_DEVICE_PREFIX = "bb_dev_"
_TOKEN_BYTES = 16  # 32 hex chars


def generate_user_token() -> str:
    """Generate a user token: bb_usr_<32-hex>."""
    return f"{_USER_PREFIX}{secrets.token_hex(_TOKEN_BYTES)}"


def generate_device_token() -> str:
    """Generate a device token: bb_dev_<32-hex>."""
    return f"{_DEVICE_PREFIX}{secrets.token_hex(_TOKEN_BYTES)}"


def hash_token(token: str) -> str:
    """SHA-256 hash a token for storage. Never store plaintext."""
    return hashlib.sha256(token.encode()).hexdigest()


def is_user_token(token: str) -> bool:
    """Check if a token has the user prefix."""
    return token.startswith(_USER_PREFIX) and len(token) == len(_USER_PREFIX) + _TOKEN_BYTES * 2


def is_device_token(token: str) -> bool:
    """Check if a token has the device prefix."""
    return (
        token.startswith(_DEVICE_PREFIX) and len(token) == len(_DEVICE_PREFIX) + _TOKEN_BYTES * 2
    )
