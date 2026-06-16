"""Network command authorization.

Server-side gate for the ``network`` command. Per-user opt-in.
"""

from __future__ import annotations

from server.storage.database import Database


class NetworkNotAllowed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def assert_network_allowed(db: Database, user_id: str) -> None:
    row = await db.fetchone(
        "SELECT network_enabled FROM users WHERE id = ?",
        (user_id,),
    )
    if row is None:
        raise NetworkNotAllowed("user_not_found")
    if not row["network_enabled"]:
        raise NetworkNotAllowed("network_disabled_for_user")
