#!/usr/bin/env python3
"""Offline CLI contract tests for ``python -m server.cli``.

Subprocess-driven against the real CLI entrypoint, mirroring the runnable
tests in this repo. Each test gets a fresh temp SQLite DB (no live server):
the first ``create-user`` call auto-creates + migrates the DB via
``server.cli._init_db`` → ``apply_migrations``.

Run:
    .venv/bin/python -m pytest tests/test_cli.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

import pytest

# Two levels up from this file == project root (mirrors test_devices_api.py).
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def cli():
    """Yield a ``run(*args)`` helper bound to a throwaway DB.

    Creates a temp dir + ``config.toml`` pointing at a temp DB, then returns a
    callable that runs ``python -m server.cli <args>`` with ``BB_CONFIG`` set,
    ``cwd=ROOT`` and ``sys.executable``, capturing (returncode, stdout, stderr).
    The temp dir (and DB) is removed when the test finishes.
    """
    with tempfile.TemporaryDirectory(prefix="bb_cli_test_") as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        config_path = os.path.join(tmpdir, "config.toml")
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.write("[database]\n")
            fh.write(f'path = "{db_path}"\n')

        def run(*args: str) -> tuple[int, str, str]:
            env = os.environ.copy()
            env["BB_CONFIG"] = config_path
            proc = subprocess.run(
                [sys.executable, "-m", "server.cli", *args],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
            )
            return proc.returncode, proc.stdout, proc.stderr

        yield run


def _create_user(run, name: str = "alice") -> str:
    """Helper: create a user, assert the token contract, return the token."""
    rc, out, err = run("create-user", name)
    assert rc == 0, f"create-user rc={rc} err={err!r}"
    assert err == "", f"expected empty stderr, got {err!r}"
    token = out.strip()
    assert re.match(r"^bb_usr_[0-9a-f]+$", token), f"unexpected token line: {out!r}"
    return token


def test_create_user_prints_only_token(cli):
    rc, out, err = cli("create-user", "alice")
    assert rc == 0, f"rc={rc} err={err!r}"
    assert err == "", f"stderr not empty: {err!r}"
    lines = out.splitlines()
    assert len(lines) == 1, f"expected single line, got {lines!r}"
    assert re.match(r"^bb_usr_[0-9a-f]+$", lines[0]), f"token line: {lines[0]!r}"


def test_create_pairing_code_prints_only_code(cli):
    _create_user(cli, "alice")
    rc, out, err = cli("create-pairing-code", "alice")
    assert rc == 0, f"rc={rc} err={err!r}"
    assert err == "", f"stderr not empty: {err!r}"
    lines = out.splitlines()
    assert len(lines) == 1, f"expected single line, got {lines!r}"
    code = lines[0]
    assert len(code) == 6, f"code len != 6: {code!r}"
    assert all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for ch in code), f"code: {code!r}"


def test_create_pairing_code_unknown_user_errors(cli):
    rc, out, err = cli("create-pairing-code", "nobody")
    assert rc == 1, f"expected rc 1, got {rc} (out={out!r} err={err!r})"
    assert "not found" in err, f"expected 'not found' in stderr: {err!r}"


def test_grant_and_revoke_network(cli):
    _create_user(cli, "alice")

    rc, out, err = cli("grant-network", "alice")
    assert rc == 0, f"grant-network rc={rc} err={err!r}"

    rc, out, _ = cli("show-user", "alice")
    assert rc == 0
    user = json.loads(out)["user"]
    assert user["network_enabled"], f"expected network_enabled truthy: {user}"

    rc, out, err = cli("revoke-network", "alice")
    assert rc == 0, f"revoke-network rc={rc} err={err!r}"

    rc, out, _ = cli("show-user", "alice")
    assert rc == 0
    user = json.loads(out)["user"]
    assert not user["network_enabled"], f"expected network_enabled falsy: {user}"


def test_grant_evaluate_with_domains(cli):
    _create_user(cli, "alice")

    rc, out, err = cli("grant-evaluate", "alice", "--domains", "*")
    assert rc == 0, f"grant-evaluate rc={rc} err={err!r}"

    rc, out, _ = cli("show-user", "alice")
    assert rc == 0
    user = json.loads(out)["user"]
    assert user["evaluate_enabled"], f"expected evaluate_enabled truthy: {user}"
    assert user["evaluate_domains"] == "*", f"evaluate_domains: {user.get('evaluate_domains')!r}"

    rc, out, err = cli("grant-evaluate", "alice", "--disable")
    assert rc == 0, f"grant-evaluate --disable rc={rc} err={err!r}"

    rc, out, _ = cli("show-user", "alice")
    assert rc == 0
    user = json.loads(out)["user"]
    assert not user["evaluate_enabled"], f"expected evaluate_enabled falsy: {user}"


def test_json_flag_both_positions(cli):
    _create_user(cli, "alice")

    rc, out, err = cli("--json", "list-users")
    assert rc == 0, f"--json list-users rc={rc} err={err!r}"
    assert isinstance(json.loads(out), list), f"expected list, got {out!r}"

    rc, out, err = cli("list-users", "--json")
    assert rc == 0, f"list-users --json rc={rc} err={err!r}"
    assert isinstance(json.loads(out), list), f"expected list, got {out!r}"


def test_show_user_has_network_key(cli):
    _create_user(cli, "alice")
    rc, out, _ = cli("show-user", "alice")
    assert rc == 0
    user = json.loads(out)["user"]
    assert "network_enabled" in user, f"'network_enabled' missing from user dict: {user}"


def test_list_sessions_empty_json(cli):
    _create_user(cli, "alice")
    rc, out, err = cli("list-sessions", "--json")
    assert rc == 0, f"rc={rc} err={err!r}"
    assert json.loads(out) == [], f"expected [], got {out!r}"


def test_revoke_user(cli):
    _create_user(cli, "alice")

    rc, out, err = cli("revoke-user", "alice")
    assert rc == 0, f"revoke-user rc={rc} err={err!r}"

    rc, out, _ = cli("list-users", "--json")
    assert rc == 0
    users = json.loads(out)
    alice = next((u for u in users if u["name"] == "alice"), None)
    assert alice is not None, f"alice missing from list-users: {users}"
    assert alice["is_active"] == 0, f"expected is_active 0, got {alice['is_active']!r}"

    # And absent from --active-only.
    rc, out, _ = cli("list-users", "--active-only", "--json")
    assert rc == 0
    active = json.loads(out)
    assert all(u["name"] != "alice" for u in active), f"alice still in --active-only: {active}"


def test_fresh_db_has_network_enabled(tmp_path):
    """Schema regression guard: a freshly migrated DB must have users.network_enabled."""
    import asyncio

    from server.storage.database import Database
    from server.storage.migrations import apply_migrations

    db_path = tmp_path / "fresh.db"

    async def _build() -> list[str]:
        db = Database(str(db_path))
        await db.initialize()
        await apply_migrations(db)
        rows = await db.fetchall("PRAGMA table_info(users)")
        cols = [r["name"] for r in rows]
        await db.close()
        return cols

    cols = asyncio.run(_build())
    assert "network_enabled" in cols, f"network_enabled missing from users schema: {cols}"

