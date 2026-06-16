"""Apply database schema on startup; idempotent migrations."""

from __future__ import annotations

from pathlib import Path

from server.storage.database import Database

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def _column_exists(db: Database, table: str, column: str) -> bool:
    rows = await db.fetchall(f"PRAGMA table_info({table})")
    return any(r["name"] == column for r in rows)


async def apply_migrations(db: Database) -> None:
    """Apply schema.sql if tables are missing, then run idempotent ALTERs.

    Two paths:
      - Fresh DB (users table missing): run schema.sql which already contains
        all columns; skip the per-version ALTERs to avoid "duplicate column"
        on a connection whose schema cache is stale post-executescript.
      - Existing DB: run idempotent ALTERs guarded by PRAGMA table_info.
    """
    row = await db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    )
    if row is None:
        schema_sql = SCHEMA_PATH.read_text()
        await db.executescript(schema_sql)
        return  # schema.sql is canonical for fresh installs

    # ---- per-version ALTERs (idempotent) ----

    # v0.2.x: per-user evaluate permission (off by default)
    if not await _column_exists(db, "users", "evaluate_enabled"):
        await db.execute(
            "ALTER TABLE users ADD COLUMN evaluate_enabled INTEGER NOT NULL DEFAULT 0"
        )
    if not await _column_exists(db, "users", "evaluate_domains"):
        # Comma-separated domain allowlist; supports leading "*." for subdomains.
        # Empty string = no domain allowed (i.e. evaluate effectively disabled).
        # "*" = allow on any URL (use sparingly).
        await db.execute(
            "ALTER TABLE users ADD COLUMN evaluate_domains TEXT NOT NULL DEFAULT ''"
        )

    # v0.4.x: device meta (ext_version, UA, platform, ...) for force-upgrade
    # routing and audit. Updated on every successful WS reconnect.
    if not await _column_exists(db, "devices", "meta_json"):
        await db.execute(
            "ALTER TABLE devices ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'"
        )

    # v0.5.0: short-lived sids for the audit web page (replaces user_token in URL)
    audit_sessions_row = await db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_sessions'"
    )
    if audit_sessions_row is None:
        await db.execute(
            """
            CREATE TABLE audit_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                revoked_at TEXT
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_sessions_user_id ON audit_sessions(user_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_sessions_expires_at ON audit_sessions(expires_at)"
        )

    # v0.8.0: per-user network monitoring permission
    if not await _column_exists(db, "users", "network_enabled"):
        await db.execute(
            "ALTER TABLE users ADD COLUMN network_enabled INTEGER NOT NULL DEFAULT 0"
        )
