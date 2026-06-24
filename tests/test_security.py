#!/usr/bin/env python3
"""Security regression tests — lock the Batch-1 fixes into CI.

Each test asserts the SECURE behavior so that reverting the corresponding fix
makes it FAIL (verified via mutation testing). Offline: builds throwaway
SQLite DBs and calls the real handlers/classes directly (no live server, no
TestClient — endpoint coroutines are invoked directly so everything stays on
one event loop, avoiding the aiosqlite cross-loop gotcha).

Covers: AUTHZ-2 (/admin/audit admin-only), WS-1 (owner-bound command result),
WS-2 (identity-aware disconnect), PROV-1 (atomic single-use redeem),
PROV-7 (generic pairing error), LOG-1 (pairing codes hashed at rest).

AUTHZ-1 (cross-user /command IDOR) is already locked in tests/test_pause_gate.py
(test_idor_other_users_device_404 / test_offline_device_404_unknown).

Run: .venv/bin/python -m pytest tests/test_security.py -q
"""

from __future__ import annotations

import asyncio
import inspect
import json
import pathlib
import sys

import pytest
from fastapi import HTTPException

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import admin_ops  # noqa: E402
from server.auth.tokens import hash_token  # noqa: E402
from server.config import load_config  # noqa: E402
from server.storage.database import Database  # noqa: E402
from server.storage.migrations import apply_migrations  # noqa: E402
from server.ws.connection_manager import ConnectionManager  # noqa: E402


async def _fresh_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "x.db"))
    await db.initialize()
    await apply_migrations(db)
    return db


class _StubWS:
    """Minimal WebSocket stand-in: records sent frames and close() calls."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: list[tuple[int, str]] = []

    async def send_text(self, s: str) -> None:
        self.sent.append(s)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


class _FakeClient:
    host = "127.0.0.1"


class _FakeRequest:
    """Enough of starlette.Request for pair_device: .client.host."""

    client = _FakeClient()


# --------------------------------------------------------------------------
# AUTHZ-2 — /admin/audit is admin-only (no Bearer bypass)
# --------------------------------------------------------------------------

def test_authz2_audit_endpoint_has_no_bearer_param():
    """Structural guard: the zero-validation `authorization` Bearer param is
    gone; the only credential is x_admin_token."""
    from server.api.audit import get_audit_log

    params = inspect.signature(get_audit_log).parameters
    assert "authorization" not in params, list(params)
    assert "x_admin_token" in params


async def test_authz2_admin_audit_requires_admin_token(tmp_path):
    from server.api import admin as admin_mod
    from server.api import audit as audit_mod
    from server.audit.logger import AuditLogger
    from server.limiter import RateLimiter

    db = await _fresh_db(tmp_path)
    try:
        cfg = load_config()
        token_file = tmp_path / ".admin_token"
        token_file.write_text("bb_adm_testtoken")
        audit_mod.init_audit_router(AuditLogger(db))
        # sets admin._admin_token_path that get_audit_log -> _check_admin reads
        admin_mod.init_admin_router(
            admin_token_path=token_file, rate_limiter=RateLimiter(cfg.limits)
        )

        # No credential at all -> 401 (regression: old code returned 200 here
        # for any "Bearer <anything>").
        with pytest.raises(HTTPException) as e_none:
            await audit_mod.get_audit_log(limit=10, offset=0, x_admin_token=None)
        assert e_none.value.status_code == 401

        # Wrong admin token -> 403.
        with pytest.raises(HTTPException) as e_bad:
            await audit_mod.get_audit_log(limit=10, offset=0, x_admin_token="bb_adm_wrong")
        assert e_bad.value.status_code == 403

        # Correct admin token -> 200-equivalent dict with entries.
        res = await audit_mod.get_audit_log(limit=10, offset=0, x_admin_token="bb_adm_testtoken")
        assert "entries" in res and "count" in res
    finally:
        await db.close()


# --------------------------------------------------------------------------
# WS-1 — command-result futures are owner-bound
# --------------------------------------------------------------------------

async def test_ws1_cross_device_result_forgery_blocked():
    cm = ConnectionManager()
    vic_ws, atk_ws = _StubWS(), _StubWS()
    await cm.connect("vic-dev", "u-vic", "vic", vic_ws)
    await cm.connect("atk-dev", "u-atk", "atk", atk_ws)

    task = asyncio.create_task(
        cm.send_and_wait("vic-dev", {"type": "command", "cmd": "snapshot"}, 5)
    )
    await asyncio.sleep(0.05)  # let send_and_wait emit the frame + register future
    assert vic_ws.sent, "command was not sent to the victim socket"
    msg_id = json.loads(vic_ws.sent[0])["msg_id"]

    # Attacker (different device) must NOT be able to resolve the victim's future.
    forged = {"type": "result", "msg_id": msg_id, "data": {"forged": "attacker"}}
    assert cm.resolve_command("atk-dev", msg_id, forged) is False
    assert not task.done(), "victim future was wrongly resolved by the attacker"

    # The owning device resolves it normally.
    real = {"type": "result", "msg_id": msg_id, "data": {"ok": 1}}
    assert cm.resolve_command("vic-dev", msg_id, real) is True
    result = await asyncio.wait_for(task, 2)
    assert result == {"ok": 1}


# --------------------------------------------------------------------------
# WS-2 — identity-aware disconnect (reconnect doesn't orphan the new socket)
# --------------------------------------------------------------------------

async def test_ws2_reconnect_does_not_orphan_new_socket():
    cm = ConnectionManager()
    ws1, ws2 = _StubWS(), _StubWS()
    conn_a = await cm.connect("d1", "u1", "n", ws1)
    conn_b = await cm.connect("d1", "u1", "n", ws2)  # reconnect under same id

    assert any(code == 4003 for code, _ in ws1.closed), ws1.closed  # old evicted

    # Stale handler teardown for the OLD conn must not remove the NEW one.
    await cm.disconnect("d1", conn_a)
    assert cm.is_connected("d1") is True
    assert cm.get("d1") is conn_b

    # The new conn's own teardown does remove it.
    await cm.disconnect("d1", conn_b)
    assert cm.is_connected("d1") is False


# --------------------------------------------------------------------------
# PROV-1 / PROV-7 — atomic single-use redeem + generic error
# --------------------------------------------------------------------------

async def test_prov1_prov7_single_use_and_generic_error(tmp_path):
    from server.api import pairing as pairing_mod
    from server.audit.logger import AuditLogger

    db = await _fresh_db(tmp_path)
    try:
        cfg = load_config()
        user = await admin_ops.create_user(db, "alice")
        info = await admin_ops.create_pairing_code(db, cfg, user["user_id"])
        code = info["code"]
        pairing_mod.init_pairing_router(db, AuditLogger(db))

        # First redeem succeeds.
        resp = await pairing_mod.pair_device(
            pairing_mod.PairRequest(pairing_code=code, device_name="d1"), _FakeRequest()
        )
        assert resp.device_token.startswith("bb_dev_")

        # Second redeem of the same (now-used) code -> generic 400.
        with pytest.raises(HTTPException) as e_used:
            await pairing_mod.pair_device(
                pairing_mod.PairRequest(pairing_code=code, device_name="d2"), _FakeRequest()
            )
        assert e_used.value.status_code == 400

        # Bogus code -> 400 with the SAME generic detail (no enumeration oracle).
        with pytest.raises(HTTPException) as e_bad:
            await pairing_mod.pair_device(
                pairing_mod.PairRequest(pairing_code="ZZZZZZ", device_name="d3"), _FakeRequest()
            )
        assert e_bad.value.status_code == 400
        assert e_used.value.detail == e_bad.value.detail  # PROV-7

        # Exactly ONE device minted from the single-use code (PROV-1).
        rows = await db.fetchall(
            "SELECT id FROM devices WHERE user_id = ?", (user["user_id"],)
        )
        assert len(rows) == 1, rows
    finally:
        await db.close()


def test_prov1_redeem_update_is_atomic():
    """Structural guard for the TOCTOU fix: the redeem UPDATE must claim the
    row conditionally (``... AND used = 0``). The sequential test above is
    caught by the earlier read-check, so this guards the atomic clause itself
    — it goes red if someone drops the condition (mutation-verified)."""
    from server.api import pairing as pairing_mod

    src = inspect.getsource(pairing_mod.pair_device)
    assert "AND used = 0" in src, "redeem UPDATE lost its conditional single-use claim"


# --------------------------------------------------------------------------
# LOG-1 — pairing codes hashed at rest
# --------------------------------------------------------------------------

async def test_log1_pairing_codes_hashed_at_rest(tmp_path):
    db = await _fresh_db(tmp_path)
    try:
        cfg = load_config()
        cols = [r["name"] for r in await db.fetchall("PRAGMA table_info(pairing_codes)")]
        assert "code_hash" in cols and "code" not in cols, cols

        user = await admin_ops.create_user(db, "bob")
        info = await admin_ops.create_pairing_code(db, cfg, user["user_id"])
        code = info["code"]

        row = await db.fetchone(
            "SELECT * FROM pairing_codes WHERE code_hash = ?", (hash_token(code),)
        )
        assert row is not None, "code not stored under its hash"
        rowd = dict(row)
        assert rowd["code_hash"] == hash_token(code)
        # The plaintext code must not appear verbatim in any column.
        assert code not in [str(v) for v in rowd.values()]
    finally:
        await db.close()
