"""CLI commands for browser-bridge administration."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from typing import Any

from server.admin_ops import (
    create_pairing_code as op_create_pairing_code,
)
from server.admin_ops import (
    create_user as op_create_user,
)
from server.admin_ops import (
    grant_evaluate as op_grant_evaluate,
)
from server.admin_ops import (
    grant_network as op_grant_network,
)
from server.admin_ops import (
    revoke_device as op_revoke_device,
)
from server.admin_ops import (
    revoke_evaluate as op_revoke_evaluate,
)
from server.admin_ops import (
    revoke_network as op_revoke_network,
)
from server.admin_ops import (
    revoke_user as op_revoke_user,
)
from server.cli_fmt import print_json, render_table, should_use_json
from server.config import AppConfig, load_config
from server.storage.database import Database
from server.storage.migrations import apply_migrations

# Module-level args reference, set in main() before dispatch.
_args: argparse.Namespace | None = None


async def _init_db() -> tuple[Database, AppConfig]:
    config = load_config()
    db = Database(config.database.path)
    await db.initialize()
    await apply_migrations(db)
    return db, config


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


async def _resolve_user(db: Database, user_ref: str) -> dict | None:
    """Look up a user by id (uuid) or name. Returns row dict or None."""
    cols = (
        "id, name, is_active, evaluate_enabled, evaluate_domains, "
        "network_enabled, created_at, updated_at"
    )
    # try id first
    row = await db.fetchone(
        f"SELECT {cols} FROM users WHERE id = ?",
        (user_ref,),
    )
    if row:
        return dict(row)
    # fall back to most-recent matching name
    row = await db.fetchone(
        f"SELECT {cols} FROM users WHERE name = ? ORDER BY created_at DESC LIMIT 1",
        (user_ref,),
    )
    return dict(row) if row else None


# ---------- existing ops ----------


async def _create_user(name: str) -> None:
    db, _ = await _init_db()
    res = await op_create_user(db, name)
    await db.close()
    print(res["token"])


async def _create_pairing_code(user_ref: str) -> None:
    db, config = await _init_db()

    user = await _resolve_user(db, user_ref)
    if user is None:
        print(f"Error: user {user_ref!r} not found", file=sys.stderr)
        await db.close()
        sys.exit(1)
    if not user["is_active"]:
        print(f"Error: user {user['name']!r} is revoked", file=sys.stderr)
        await db.close()
        sys.exit(1)

    res = await op_create_pairing_code(db, config, user["id"])
    await db.close()

    print(res["code"])


# ---------- new: read-side ----------


async def _list_users(active_only: bool, as_json: bool) -> None:
    db, _ = await _init_db()
    sql = (
        "SELECT u.id, u.name, u.is_active, u.evaluate_enabled, u.evaluate_domains, "
        "  u.created_at, u.updated_at, "
        "  (SELECT COUNT(*) FROM devices d WHERE d.user_id = u.id AND d.is_active = 1) "
        "    AS active_devices, "
        "  (SELECT COUNT(*) FROM devices d WHERE d.user_id = u.id) AS total_devices "
        "FROM users u"
    )
    if active_only:
        sql += " WHERE u.is_active = 1"
    sql += " ORDER BY u.created_at DESC"
    rows = await db.fetchall(sql)
    await db.close()
    data = [dict(r) for r in rows]
    if as_json:
        print_json(data)
    else:
        for d in data:
            d["is_active"] = "active" if d["is_active"] else "revoked"
            d["devices"] = f"{d.pop('active_devices')}/{d.pop('total_devices')}"
        render_table(data, [
            ("name", "Name"),
            ("is_active", "Status"),
            ("devices", "Devices"),
            ("evaluate_enabled", "Eval"),
            ("created_at", "Created"),
        ], title="Users")


async def _show_user(user_ref: str) -> None:
    db, _ = await _init_db()
    user = await _resolve_user(db, user_ref)
    if user is None:
        print(f"Error: user {user_ref!r} not found", file=sys.stderr)
        await db.close()
        sys.exit(1)
    devices = await db.fetchall(
        "SELECT id, name, is_active, last_seen_at, created_at FROM devices "
        "WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    )
    sessions = await db.fetchall(
        "SELECT id, device_id, state, created_at, last_activity_at "
        "FROM sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
        (user["id"],),
    )
    pending_codes = await db.fetchall(
        "SELECT code, expires_at, used FROM pairing_codes "
        "WHERE user_id = ? AND used = 0 AND expires_at > datetime('now') "
        "ORDER BY expires_at DESC",
        (user["id"],),
    )
    await db.close()
    _print_json(
        {
            "user": user,
            "devices": [dict(r) for r in devices],
            "recent_sessions": [dict(r) for r in sessions],
            "active_pairing_codes": [dict(r) for r in pending_codes],
        }
    )


async def _list_devices(user_ref: str | None, active_only: bool, as_json: bool) -> None:
    db, _ = await _init_db()
    if user_ref:
        user = await _resolve_user(db, user_ref)
        if user is None:
            print(f"Error: user {user_ref!r} not found", file=sys.stderr)
            await db.close()
            sys.exit(1)
        params = (user["id"],)
        sql = (
            "SELECT d.id, d.name, d.is_active, d.last_seen_at, d.created_at, "
            "  d.meta_json, u.name AS user_name "
            "FROM devices d JOIN users u ON u.id = d.user_id "
            "WHERE d.user_id = ?"
        )
    else:
        params = ()
        sql = (
            "SELECT d.id, d.name, d.is_active, d.last_seen_at, d.created_at, "
            "  d.meta_json, u.name AS user_name "
            "FROM devices d JOIN users u ON u.id = d.user_id"
        )
    if active_only:
        sql += " AND d.is_active = 1" if "WHERE" in sql else " WHERE d.is_active = 1"
    sql += " ORDER BY d.last_seen_at DESC NULLS LAST, d.created_at DESC"
    rows = await db.fetchall(sql, params)
    await db.close()
    out = []
    for r in rows:
        d = dict(r)
        meta_raw = d.pop("meta_json", "") or "{}"
        try:
            d["meta"] = json.loads(meta_raw)
        except Exception:
            d["meta"] = {"_raw": meta_raw}
        out.append(d)
    if as_json:
        print_json(out)
    else:
        for d in out:
            d["is_active"] = "active" if d["is_active"] else "revoked"
            d["ext_version"] = (d.get("meta") or {}).get("ext_version", "")
        render_table(out, [
            ("name", "Name"),
            ("user_name", "User"),
            ("is_active", "Status"),
            ("ext_version", "ExtVer"),
            ("last_seen_at", "Last Seen"),
        ], title="Devices")


async def _list_pairing_codes(active_only: bool, as_json: bool) -> None:
    db, _ = await _init_db()
    sql = (
        "SELECT pc.code, pc.expires_at, pc.used, pc.used_by_device_id, "
        "  pc.created_at, u.name AS user_name, u.id AS user_id "
        "FROM pairing_codes pc JOIN users u ON u.id = pc.user_id"
    )
    if active_only:
        sql += " WHERE pc.used = 0 AND pc.expires_at > datetime('now')"
    sql += " ORDER BY pc.created_at DESC LIMIT 50"
    rows = await db.fetchall(sql)
    await db.close()
    data = [dict(r) for r in rows]
    if as_json:
        print_json(data)
    else:
        for d in data:
            d["status"] = "used" if d["used"] else "active"
        render_table(data, [
            ("code", "Code"),
            ("user_name", "User"),
            ("status", "Status"),
            ("expires_at", "Expires"),
        ], title="Pairing Codes")


async def _list_sessions(active_only: bool, as_json: bool) -> None:
    db, _ = await _init_db()
    sql = (
        "SELECT s.id, s.user_id, u.name AS user_name, s.device_id, s.state, "
        "  s.created_at, s.activated_at, s.last_activity_at, s.closed_at, "
        "  s.close_reason "
        "FROM sessions s JOIN users u ON u.id = s.user_id"
    )
    if active_only:
        sql += " WHERE s.state IN ('created','active')"
    sql += " ORDER BY s.created_at DESC LIMIT 100"
    rows = await db.fetchall(sql)
    await db.close()
    data = [dict(r) for r in rows]
    if as_json:
        print_json(data)
    else:
        render_table(data, [
            ("id", "Session ID"),
            ("user_name", "User"),
            ("state", "State"),
            ("last_activity_at", "Last Activity"),
        ], title="Sessions")


async def _stats(as_json: bool) -> None:
    db, _ = await _init_db()
    users_total = (await db.fetchone(
        "SELECT COUNT(*) AS n FROM users"
    ))["n"]
    users_active = (await db.fetchone(
        "SELECT COUNT(*) AS n FROM users WHERE is_active = 1"
    ))["n"]
    devices_total = (await db.fetchone(
        "SELECT COUNT(*) AS n FROM devices"
    ))["n"]
    devices_active = (await db.fetchone(
        "SELECT COUNT(*) AS n FROM devices WHERE is_active = 1"
    ))["n"]
    sessions_active = (await db.fetchone(
        "SELECT COUNT(*) AS n FROM sessions WHERE state IN ('created','active')"
    ))["n"]
    cmds_today = (await db.fetchone(
        "SELECT COUNT(*) AS n FROM commands "
        "WHERE created_at >= datetime('now','-1 day')"
    ))["n"]
    cmds_failed = (await db.fetchone(
        "SELECT COUNT(*) AS n FROM commands "
        "WHERE created_at >= datetime('now','-1 day') AND status IN ('failed','timeout')"
    ))["n"]
    active_codes = (await db.fetchone(
        "SELECT COUNT(*) AS n FROM pairing_codes "
        "WHERE used = 0 AND expires_at > datetime('now')"
    ))["n"]
    await db.close()
    data = {
        "users": {"total": users_total, "active": users_active},
        "devices": {"total": devices_total, "active": devices_active},
        "sessions": {"active_or_created": sessions_active},
        "commands_24h": {"total": cmds_today, "failed_or_timeout": cmds_failed},
        "pairing_codes_active": active_codes,
    }
    if as_json:
        print_json(data)
    else:
        from server.cli_fmt import c
        print(c("Stats", "bold"))
        print(f"  Users:    {users_active} active / {users_total} total")
        print(f"  Devices:  {devices_active} active / {devices_total} total")
        print(f"  Sessions: {sessions_active} active")
        print(f"  Cmds 24h: {cmds_today} total, {cmds_failed} failed/timeout")
        print(f"  Codes:    {active_codes} pending")


# ---------- new: write-side ----------


async def _revoke_user(user_ref: str) -> None:
    db, _ = await _init_db()
    user = await _resolve_user(db, user_ref)
    if user is None:
        print(f"Error: user {user_ref!r} not found", file=sys.stderr)
        await db.close()
        sys.exit(1)
    await op_revoke_user(db, user["id"])
    await db.close()
    _print_json({"revoked_user": user["name"], "user_id": user["id"]})


async def _revoke_device(device_id: str) -> None:
    db, _ = await _init_db()
    row = await db.fetchone(
        "SELECT id, name, user_id FROM devices WHERE id = ?", (device_id,)
    )
    if row is None:
        print(f"Error: device {device_id!r} not found", file=sys.stderr)
        await db.close()
        sys.exit(1)
    await op_revoke_device(db, device_id)
    await db.close()
    _print_json({"revoked_device": dict(row)})


async def _grant_evaluate(user_ref: str, domains: str | None, disable: bool) -> None:
    db, _ = await _init_db()
    user = await _resolve_user(db, user_ref)
    if user is None:
        print(f"Error: user {user_ref!r} not found", file=sys.stderr)
        await db.close()
        sys.exit(1)
    if disable:
        await op_revoke_evaluate(db, user["id"])
    else:
        await op_grant_evaluate(db, user["id"], domains or "")
    after = await db.fetchone(
        "SELECT id, name, evaluate_enabled, evaluate_domains FROM users WHERE id = ?",
        (user["id"],),
    )
    await db.close()
    _print_json(dict(after))


async def _grant_network(user_ref: str) -> None:
    db, _ = await _init_db()
    user = await _resolve_user(db, user_ref)
    if user is None:
        print(f"Error: user {user_ref!r} not found", file=sys.stderr)
        await db.close()
        sys.exit(1)
    await op_grant_network(db, user["id"])
    after = await db.fetchone(
        "SELECT id, name, network_enabled FROM users WHERE id = ?",
        (user["id"],),
    )
    await db.close()
    _print_json(dict(after))


async def _revoke_network(user_ref: str) -> None:
    db, _ = await _init_db()
    user = await _resolve_user(db, user_ref)
    if user is None:
        print(f"Error: user {user_ref!r} not found", file=sys.stderr)
        await db.close()
        sys.exit(1)
    await op_revoke_network(db, user["id"])
    after = await db.fetchone(
        "SELECT id, name, network_enabled FROM users WHERE id = ?",
        (user["id"],),
    )
    await db.close()
    _print_json(dict(after))


# ---------- arg parser ----------


async def _create_admin_token(rotate: bool) -> None:
    """Generate (or rotate) the admin token used for /api/v1/admin/* endpoints.

    Stored as plaintext in ``<db_dir>/.admin_token`` (chmod 0600).
    """
    from pathlib import Path

    config = load_config()
    db_dir = Path(config.database.path).resolve().parent
    db_dir.mkdir(parents=True, exist_ok=True)
    token_path = db_dir / ".admin_token"

    if token_path.exists() and not rotate:
        print(
            f"ERROR: {token_path} already exists. Pass --rotate to overwrite.",
            file=sys.stderr,
        )
        sys.exit(2)

    token = "bb_adm_" + secrets.token_hex(24)
    token_path.write_text(token)
    token_path.chmod(0o600)

    _print_json(
        {
            "action": "rotated" if rotate and token_path.exists() else "created",
            "path": str(token_path),
            "token": token,
            "usage": (
                f"curl -X POST -H 'X-Admin-Token: {token}' "
                f"http://127.0.0.1:<port>/api/v1/admin/reload-config"
            ),
        }
    )


async def _force_detach(device_id: str, server_url: str) -> None:
    """Dev-only: hit ``POST /api/v1/admin/devices/<device_id>/force-detach``
    on the live server.

    Equivalent to the user clicking Cancel on chrome's "is being debugged"
    yellow bar — the extension calls ``chrome.debugger.detach`` on every
    attached tab. Used by ``test_sw_lifecycle`` to exercise the active-detach
    cleanup path without needing a human on the keyboard.

    Reads the admin token from ``<db_dir>/.admin_token`` (same place
    ``create-admin-token`` writes it).
    """
    from pathlib import Path

    import httpx  # local import: cli imports stay light

    config = load_config()
    db_dir = Path(config.database.path).resolve().parent
    token_path = db_dir / ".admin_token"
    if not token_path.exists():
        print(
            f"ERROR: admin token not found at {token_path}. "
            "Run `python -m server.cli create-admin-token` first.",
            file=sys.stderr,
        )
        sys.exit(2)
    token = token_path.read_text().strip()
    if not token:
        print(f"ERROR: admin token file at {token_path} is empty.", file=sys.stderr)
        sys.exit(2)

    url = f"{server_url.rstrip('/')}/api/v1/admin/devices/{device_id}/force-detach"
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(url, headers={"X-Admin-Token": token})
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: request to {url} failed: {e}", file=sys.stderr)
        sys.exit(3)
    if resp.status_code >= 400:
        print(
            f"ERROR: server returned HTTP {resp.status_code}: {resp.text[:300]}",
            file=sys.stderr,
        )
        sys.exit(4)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"_raw": resp.text}
    _print_json(body)


def main() -> None:
    global _args
    parser = argparse.ArgumentParser(
        prog="browser-bridge-cli", description="Browser Bridge CLI"
    )
    parser.add_argument("--json", action="store_true", help="Force JSON output")
    sub = parser.add_subparsers(dest="command")

    # --json is accepted both globally (before the subcommand) and after the
    # subcommand. The per-subparser copy uses SUPPRESS as its default so that
    # *omitting* it after the subcommand never overwrites a global `--json`.
    jsonopt = argparse.ArgumentParser(add_help=False)
    jsonopt.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help="Force JSON output",
    )

    p = sub.add_parser(
        "create-user", help="Create a new user and output token", parents=[jsonopt]
    )
    p.add_argument("name", help="Display name for the user")

    p = sub.add_parser(
        "create-pairing-code", help="Generate a pairing code for a user",
        parents=[jsonopt],
    )
    p.add_argument("user", help="User id or name")

    p = sub.add_parser("list-users", help="List users", parents=[jsonopt])
    p.add_argument("--active-only", action="store_true")

    p = sub.add_parser(
        "show-user", help="Show user detail (devices/sessions/codes)",
        parents=[jsonopt],
    )
    p.add_argument("user", help="User id or name")

    p = sub.add_parser("list-devices", help="List devices", parents=[jsonopt])
    p.add_argument("--user", help="Filter by user (id or name)", default=None)
    p.add_argument("--active-only", action="store_true")

    p = sub.add_parser(
        "list-pairing-codes", help="List pairing codes (active by default)",
        parents=[jsonopt],
    )
    p.add_argument("--all", action="store_true", help="Include used/expired codes")

    p = sub.add_parser(
        "list-sessions", help="List sessions (active by default)", parents=[jsonopt]
    )
    p.add_argument("--all", action="store_true", help="Include closed sessions")

    p = sub.add_parser("stats", help="Quick deployment stats", parents=[jsonopt])

    p = sub.add_parser(
        "revoke-user", help="Revoke a user (and all their devices)", parents=[jsonopt]
    )
    p.add_argument("user", help="User id or name")

    p = sub.add_parser(
        "revoke-device", help="Revoke a single device", parents=[jsonopt]
    )
    p.add_argument("device_id", help="Device id (uuid)")

    p = sub.add_parser(
        "grant-evaluate",
        help="Enable the high-risk evaluate command for a user (with domain allowlist)",
        parents=[jsonopt],
    )
    p.add_argument("user", help="User id or name")
    p.add_argument(
        "--domains",
        default="",
        help='Comma-separated domain allowlist; supports "*.example.com" and "*" (any). '
        'Empty string still requires --disable to fully turn off.',
    )
    p.add_argument(
        "--disable",
        action="store_true",
        help="Turn evaluate off for this user.",
    )

    p = sub.add_parser(
        "grant-network",
        help="Enable the network (debugger/capture) command for a user",
        parents=[jsonopt],
    )
    p.add_argument("user", help="User id or name")

    p = sub.add_parser(
        "revoke-network", help="Disable the network command for a user",
        parents=[jsonopt],
    )
    p.add_argument("user", help="User id or name")

    p = sub.add_parser(
        "create-admin-token",
        help="Generate (or rotate) the admin token used for /api/v1/admin/* endpoints. "
        "Stored as plaintext in <db_dir>/.admin_token (chmod 0600).",
        parents=[jsonopt],
    )
    p.add_argument(
        "--rotate",
        action="store_true",
        help="Rotate even if a token already exists (default: refuse to overwrite).",
    )

    p = sub.add_parser(
        "force-detach",
        help="Dev-only: ask the extension to chrome.debugger.detach every "
        "attached tab on a device. Equivalent to the user clicking Cancel on "
        "chrome's 'is being debugged' banner. Used by tests/e2e/test_sw_lifecycle.py.",
        parents=[jsonopt],
    )
    p.add_argument("device_id", help="Device id (uuid)")
    p.add_argument(
        "--server",
        default="http://127.0.0.1:8403",
        help="Live server URL (default: http://127.0.0.1:8403).",
    )

    args = parser.parse_args()
    _args = args
    as_json = should_use_json(args)

    handlers = {
        "create-user": lambda: _create_user(args.name),
        "create-pairing-code": lambda: _create_pairing_code(args.user),
        "list-users": lambda: _list_users(args.active_only, as_json),
        "show-user": lambda: _show_user(args.user),
        "list-devices": lambda: _list_devices(args.user, args.active_only, as_json),
        "list-pairing-codes": lambda: _list_pairing_codes(not args.all, as_json),
        "list-sessions": lambda: _list_sessions(not args.all, as_json),
        "stats": lambda: _stats(as_json),
        "revoke-user": lambda: _revoke_user(args.user),
        "revoke-device": lambda: _revoke_device(args.device_id),
        "grant-evaluate": lambda: _grant_evaluate(
            args.user, args.domains, args.disable
        ),
        "grant-network": lambda: _grant_network(args.user),
        "revoke-network": lambda: _revoke_network(args.user),
        "create-admin-token": lambda: _create_admin_token(args.rotate),
        "force-detach": lambda: _force_detach(args.device_id, args.server),
    }

    if args.command in handlers:
        asyncio.run(handlers[args.command]())
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
