#!/usr/bin/env python3
"""Pause-gate unit tests for the Command API (server side).

Self-contained: builds a minimal FastAPI app wired to the real
``command_router`` with the auth dependency overridden and a fake
ConnectionManager, so it can exercise the 423 DEVICE_PAUSED gate without a
live paired extension. Mirrors the runnable-script style of
test_command_api_e2e.py (not pytest) so it works under the project venv
(which has FastAPI + TestClient but not pytest).

Usage:
    .venv/bin/python tests/test_pause_gate.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from server.api.command import init_command_router  # noqa: E402
from server.auth import dependencies as auth_deps  # noqa: E402
from server.auth.validator import AuthInfo  # noqa: E402

USER_ID = "usr_test"
DEVICE_ID = "dev_test"


class FakeConn:
    """Stand-in for ConnectionManager.DeviceConnection — only the fields the
    command router reads: user_id and paused."""

    def __init__(self, user_id: str, paused: bool = False):
        self.user_id = user_id
        self.paused = paused


class FakeConnectionManager:
    """Minimal ConnectionManager faithful to the methods command.py calls:
    connected_device_ids, get, is_connected, send_and_wait."""

    def __init__(self):
        self._conns: dict[str, FakeConn] = {}
        self.sent: list[dict] = []

    def add(self, device_id: str, user_id: str, paused: bool = False):
        self._conns[device_id] = FakeConn(user_id, paused)

    @property
    def connected_device_ids(self) -> list[str]:
        return list(self._conns.keys())

    def get(self, device_id: str) -> FakeConn | None:
        return self._conns.get(device_id)

    def is_connected(self, device_id: str) -> bool:
        return device_id in self._conns

    async def send_and_wait(self, device_id: str, msg: dict, timeout: float) -> dict:
        # Records that a command actually went downstream. The pause gate must
        # short-circuit *before* this is ever reached.
        self.sent.append(msg)
        return {"ok": True, "echo": msg.get("cmd")}


class StubRateLimiter:
    """No-op limiter — pause gate sits before rate limiting, so this keeps the
    test focused while still satisfying the router's contract."""

    def check_and_acquire(self, **kwargs) -> None:
        pass

    def release(self, **kwargs) -> None:
        pass


def _build_client(cm: FakeConnectionManager) -> TestClient:
    app = FastAPI()
    router = init_command_router(db=object(), connection_manager=cm, rate_limiter=StubRateLimiter())
    app.include_router(router, prefix="/api/v1")

    async def _fake_auth() -> AuthInfo:
        return AuthInfo(token_type="user", id=USER_ID, user_id=USER_ID, name="tester")

    app.dependency_overrides[auth_deps.require_user_auth] = _fake_auth
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}: {detail}")


def test_paused_device_rejected_423():
    cm = FakeConnectionManager()
    cm.add(DEVICE_ID, USER_ID, paused=True)
    client = _build_client(cm)

    resp = client.post(
        "/api/v1/command",
        json={"action": "navigate", "args": {"url": "https://example.com"}, "device_id": DEVICE_ID},
        headers={"Authorization": "Bearer x"},
    )
    check("paused device -> 423", resp.status_code == 423, f"got {resp.status_code}: {resp.text}")
    detail = resp.json().get("detail", {})
    check(
        "paused device -> code DEVICE_PAUSED",
        detail.get("code") == "DEVICE_PAUSED",
        f"detail={detail}",
    )
    check("paused device -> command NOT sent downstream", cm.sent == [], f"sent={cm.sent}")


def test_paused_device_auto_resolve_423():
    # device_id omitted -> server auto-resolves the single online device, then
    # the pause gate must still fire.
    cm = FakeConnectionManager()
    cm.add(DEVICE_ID, USER_ID, paused=True)
    client = _build_client(cm)

    resp = client.post(
        "/api/v1/command",
        json={"action": "snapshot", "args": {}},
        headers={"Authorization": "Bearer x"},
    )
    check(
        "paused (auto-resolved device) -> 423",
        resp.status_code == 423,
        f"got {resp.status_code}",
    )
    check("paused (auto-resolved) -> not sent", cm.sent == [], f"sent={cm.sent}")


def test_unpaused_device_passes():
    cm = FakeConnectionManager()
    cm.add(DEVICE_ID, USER_ID, paused=False)
    client = _build_client(cm)

    resp = client.post(
        "/api/v1/command",
        json={"action": "snapshot", "args": {}, "device_id": DEVICE_ID},
        headers={"Authorization": "Bearer x"},
    )
    check("unpaused device -> 200", resp.status_code == 200, f"got {resp.status_code}: {resp.text}")
    check("unpaused device -> command sent downstream", len(cm.sent) == 1, f"sent={cm.sent}")


def test_offline_device_404_unknown():
    # AUTHZ-1: an explicit device_id the caller doesn't own (or that isn't
    # connected) is indistinguishable to the caller — both return 404
    # DEVICE_NOT_FOUND so existence can't be probed. (Previously an unconnected
    # explicit device returned 502 DEVICE_OFFLINE.)
    cm = FakeConnectionManager()  # nothing connected
    client = _build_client(cm)

    resp = client.post(
        "/api/v1/command",
        json={"action": "snapshot", "args": {}, "device_id": DEVICE_ID},
        headers={"Authorization": "Bearer x"},
    )
    check("unknown explicit device -> 404", resp.status_code == 404, f"got {resp.status_code}")
    detail = resp.json().get("detail", {})
    check(
        "unknown -> code DEVICE_NOT_FOUND",
        detail.get("code") == "DEVICE_NOT_FOUND",
        f"detail={detail}",
    )


def test_idor_other_users_device_404():
    # AUTHZ-1: amy must not be able to drive bob's connected device by passing
    # its device_id explicitly. Bob's device is online but owned by someone
    # else -> 404 (not 200, not 403).
    cm = FakeConnectionManager()
    cm.add(DEVICE_ID, "usr_bob", paused=False)  # bob's device, online
    client = _build_client(cm)  # auth overridden to usr_test (amy)

    resp = client.post(
        "/api/v1/command",
        json={"action": "snapshot", "args": {}, "device_id": DEVICE_ID},
        headers={"Authorization": "Bearer x"},
    )
    check("IDOR cross-user device -> 404", resp.status_code == 404, f"got {resp.status_code}")
    detail = resp.json().get("detail", {})
    check(
        "IDOR -> code DEVICE_NOT_FOUND",
        detail.get("code") == "DEVICE_NOT_FOUND",
        f"detail={detail}",
    )
    check("IDOR -> command NOT sent downstream", cm.sent == [], f"sent={cm.sent}")


def main():
    print("\n" + "=" * 60)
    print("  Pause Gate Unit Tests (server)")
    print("=" * 60)
    for fn in (
        test_paused_device_rejected_423,
        test_paused_device_auto_resolve_423,
        test_unpaused_device_passes,
        test_offline_device_404_unknown,
        test_idor_other_users_device_404,
    ):
        print(f"\n  [{fn.__name__}]")
        fn()

    total = len(PASSED) + len(FAILED)
    print("\n" + "-" * 60)
    print(f"  TOTAL: {len(PASSED)}/{total} passed, {len(FAILED)} failed")
    print("=" * 60 + "\n")
    if FAILED:
        for name, reason in FAILED:
            print(f"    ✗ {name}: {reason}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
