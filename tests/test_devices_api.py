#!/usr/bin/env python3
"""Unit tests for GET /api/v1/devices meta serialization.

Regression guard for the bug where list_devices never selected/returned
meta_json, so ext_version (and all device meta) was always null via the API
even when the DB had it.

Self-contained: builds a minimal FastAPI app wired to the real devices_router
with a fake DB and the auth dependency overridden. Runs under the project venv
(FastAPI + TestClient), mirroring tests/test_pause_gate.py.

Usage:
    .venv/bin/python tests/test_devices_api.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from server.api.devices import init_devices_router  # noqa: E402
from server.auth import dependencies as auth_deps  # noqa: E402
from server.auth.validator import AuthInfo  # noqa: E402

USER_ID = "usr_test"


class FakeDB:
    """Returns rows shaped like aiosqlite.Row (dict()-able). Asserts the query
    actually selects meta_json — the whole point of the fix."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.last_sql = ""

    async def fetchall(self, sql: str, params: tuple = ()):
        self.last_sql = sql
        return list(self._rows)


def _build_client(rows: list[dict]) -> tuple[TestClient, FakeDB]:
    db = FakeDB(rows)
    app = FastAPI()
    app.include_router(init_devices_router(db), prefix="/api/v1")

    async def _fake_auth() -> AuthInfo:
        return AuthInfo(token_type="user", id=USER_ID, user_id=USER_ID, name="tester")

    app.dependency_overrides[auth_deps.require_any_auth] = _fake_auth
    return TestClient(app), db


PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}: {detail}")


def _base_row(**over) -> dict:
    row = {
        "id": "d1",
        "name": "Chrome Extension",
        "is_active": 1,
        "last_seen_at": "2026-06-06T15:39:44Z",
        "created_at": "2026-06-01T00:00:00Z",
        "meta_json": "{}",
    }
    row.update(over)
    return row


def test_query_selects_meta_json():
    client, db = _build_client([_base_row()])
    client.get("/api/v1/devices", headers={"Authorization": "Bearer x"})
    check("query selects meta_json", "meta_json" in db.last_sql, f"sql={db.last_sql!r}")


def test_meta_parsed_and_ext_version_hoisted():
    rows = [_base_row(meta_json='{"ext_version":"0.8.5","platform":"MacIntel"}')]
    client, _ = _build_client(rows)
    resp = client.get("/api/v1/devices", headers={"Authorization": "Bearer x"})
    check("status 200", resp.status_code == 200, resp.text)
    d = resp.json()[0]
    check("ext_version hoisted to top level", d.get("ext_version") == "0.8.5", f"{d}")
    check(
        "meta parsed to dict",
        d.get("meta", {}).get("platform") == "MacIntel",
        f"{d.get('meta')}",
    )


def test_empty_meta_yields_null_ext_version():
    client, _ = _build_client([_base_row(meta_json="{}")])
    d = client.get("/api/v1/devices", headers={"Authorization": "Bearer x"}).json()[0]
    check("empty meta -> ext_version None", d.get("ext_version") is None, f"{d}")
    check("empty meta -> meta == {}", d.get("meta") == {}, f"{d.get('meta')}")


def test_bad_json_does_not_crash():
    client, _ = _build_client([_base_row(meta_json="not-json")])
    resp = client.get("/api/v1/devices", headers={"Authorization": "Bearer x"})
    check("bad json -> still 200", resp.status_code == 200, resp.text)
    d = resp.json()[0]
    check("bad json -> meta {} fallback", d.get("meta") == {}, f"{d.get('meta')}")
    check("bad json -> ext_version None", d.get("ext_version") is None, f"{d}")


def test_null_meta_json_column():
    # meta_json column NULL (defensive — schema default is '{}', but be safe).
    client, _ = _build_client([_base_row(meta_json=None)])
    resp = client.get("/api/v1/devices", headers={"Authorization": "Bearer x"})
    check("null meta_json -> still 200", resp.status_code == 200, resp.text)
    d = resp.json()[0]
    check("null meta_json -> meta {}", d.get("meta") == {}, f"{d.get('meta')}")


def main():
    print("\n" + "=" * 60)
    print("  Devices API meta serialization tests")
    print("=" * 60)
    for fn in (
        test_query_selects_meta_json,
        test_meta_parsed_and_ext_version_hoisted,
        test_empty_meta_yields_null_ext_version,
        test_bad_json_does_not_crash,
        test_null_meta_json_column,
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
