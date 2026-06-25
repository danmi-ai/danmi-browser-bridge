#!/usr/bin/env python3
"""Onboard a new user (or re-issue pairing code) for danmi-browser-bridge.

Usage:
    onboard.py <username> [--force-new]

Behaviour:
- If user exists in DB: reuse user_id; the user_token is *not* recoverable.
  By default we reuse a saved token from data/users/<username>.token if present;
  if missing, --force-new must be set (creates a new user record + new token,
  effectively retiring the old one).
- Always issues a fresh pairing code (single-use, ~30min TTL).
- Copies the latest extension zip + renders INSTALL.md into:
    ~/.openclaw/workspace/scratch/bb-onboard-<username>-<YYMMDD-HHMM>/

Outputs the directory path + key fields to stdout.

Env:
    BB_ROOT  override repo root.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = str(_SCRIPT_DIR.parent)
ROOT = Path(os.environ.get("BB_ROOT", DEFAULT_ROOT)).resolve()

# BB_ENV picks dev vs prod paths/url. Defaults to prod for back-compat with
# the existing onboard flow used in IM messages.
BB_ENV = os.environ.get("BB_ENV", "prod").strip().lower() or "prod"
if BB_ENV not in {"prod", "dev"}:
    BB_ENV = "prod"

if BB_ENV == "dev":
    DB_FILE = ROOT / "data-dev" / "browser_bridge.db"
    USER_TOKENS_DIR = ROOT / "data-dev" / "users"
    SERVER_URL = os.environ.get("BB_SERVER_URL", "http://127.0.0.1:8413")
else:
    DB_FILE = ROOT / "data" / "browser_bridge.db"
    USER_TOKENS_DIR = ROOT / "data" / "users"
    SERVER_URL = os.environ.get("BB_SERVER_URL", "http://127.0.0.1:8404")

EXTENSION_DIR = ROOT / "extension"
# Server CLI must be invoked with the python that has its deps installed.
SERVER_PYTHON = os.environ.get("BB_SERVER_PYTHON", "/usr/bin/python3.11")

SKILL_DIR = Path(__file__).resolve().parent.parent
TMPL = SKILL_DIR / "assets" / "INSTALL.md.tmpl"
# Onboard packages live under the skill's own runtime dir for easy maintenance.
# Dev packages get an explicit "-dev" suffix to avoid stomping prod.
_DEFAULT_ONBOARD = SKILL_DIR / ("runtime/onboard-dev" if BB_ENV == "dev" else "runtime/onboard")
DELIVERY_BASE = Path(
    os.environ.get("BB_ONBOARD_DIR", str(_DEFAULT_ONBOARD))
).expanduser()


def _db():
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn


def _find_user(username: str) -> str | None:
    with _db() as c:
        row = c.execute(
            "SELECT id FROM users WHERE name = ? ORDER BY created_at DESC LIMIT 1",
            (username,),
        ).fetchone()
    return row["id"] if row else None


def _create_user_via_cli(username: str) -> str:
    """Run server.cli create-user; capture token; resolve user_id from DB."""
    # CLI prints just the token. Then pick the most-recent user with this name.
    proc = subprocess.run(
        [SERVER_PYTHON, "-m", "server.cli", "create-user", username],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    token = proc.stdout.strip()
    if not token.startswith("bb_usr_"):
        raise RuntimeError(f"create-user output unexpected: {token!r}")
    user_id = _find_user(username)
    if not user_id:
        raise RuntimeError(f"created user {username!r} but cannot find row in DB")

    USER_TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    tok_file = USER_TOKENS_DIR / f"{username}.token"
    tok_file.write_text(token)
    tok_file.chmod(0o600)
    return token


def _read_saved_token(username: str) -> str | None:
    p = USER_TOKENS_DIR / f"{username}.token"
    if not p.exists():
        return None
    s = p.read_text().strip()
    return s if s.startswith("bb_usr_") else None


def _create_pairing_code(user_id: str) -> str:
    proc = subprocess.run(
        [SERVER_PYTHON, "-m", "server.cli", "create-pairing-code", user_id],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    code = proc.stdout.strip()
    if len(code) != 6:
        raise RuntimeError(f"create-pairing-code output unexpected: {code!r}")
    return code


def _build_extension_zip(out_zip: Path) -> None:
    """Zip the live extension/ dir (canonical source-of-truth)."""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    if out_zip.exists():
        out_zip.unlink()
    # use python's zipfile so we don't depend on system zip
    import zipfile

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in EXTENSION_DIR.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(EXTENSION_DIR))


def _render_install(
    *,
    username: str,
    server_url: str,
    pairing_code: str,
    expires_at_local: str,
    out_path: Path,
) -> None:
    if not TMPL.exists():
        raise RuntimeError(f"missing template: {TMPL}")
    txt = TMPL.read_text()
    txt = (
        txt.replace("{{USERNAME}}", username)
        .replace("{{SERVER_URL}}", server_url)
        .replace("{{PAIRING_CODE}}", pairing_code)
        .replace("{{EXPIRES_AT}}", expires_at_local)
    )
    out_path.write_text(txt)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bb-onboard", description=__doc__)
    p.add_argument("username", help="display name (e.g. alice)")
    p.add_argument(
        "--force-new",
        action="store_true",
        help="force a fresh user record + new token even if username exists",
    )
    args = p.parse_args(argv)

    username = args.username
    user_id: str | None = None
    user_token: str | None = None

    existing = _find_user(username)
    if existing and not args.force_new:
        saved = _read_saved_token(username)
        if saved:
            user_id, user_token = existing, saved
            print(
                f"# reusing existing user {username} ({user_id}) and saved token",
                file=sys.stderr,
            )
        else:
            print(
                f"# user {username} ({existing}) exists but no saved token; "
                f"re-run with --force-new to issue a fresh user record",
                file=sys.stderr,
            )
            return 2
    else:
        # create new
        user_token = _create_user_via_cli(username)
        user_id = _find_user(username)
        if not user_id:
            print("create-user succeeded but user_id lookup failed", file=sys.stderr)
            return 1
        print(f"# created user {username} ({user_id})", file=sys.stderr)

    code = _create_pairing_code(user_id)

    # 30 min TTL by default — read from server config.toml.
    # We just say "~30 min"; precise expiry queryable via DB.
    with _db() as c:
        row = c.execute(
            "SELECT expires_at FROM pairing_codes WHERE user_id=? ORDER BY expires_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    expires_iso = row["expires_at"] if row else ""
    # render in Asia/Shanghai for the user
    expires_local = ""
    if expires_iso:
        try:
            dt = datetime.fromisoformat(expires_iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            # naive: shift +08:00 manually so we don't need pytz
            from datetime import timedelta

            local = dt.astimezone(timezone(timedelta(hours=8)))
            expires_local = local.strftime("%Y-%m-%d %H:%M:%S CST")
        except Exception:
            expires_local = expires_iso

    ts = datetime.now().strftime("%y%m%d-%H%M")
    out_dir = DELIVERY_BASE / f"bb-onboard-{username}-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    zip_path = out_dir / "danmi-browser-bridge-extension.zip"
    _build_extension_zip(zip_path)

    install_path = out_dir / "INSTALL.md"
    _render_install(
        username=username,
        server_url=SERVER_URL,
        pairing_code=code,
        expires_at_local=expires_local or "~30 minutes from now",
        out_path=install_path,
    )

    summary = {
        "username": username,
        "user_id": user_id,
        "user_token": user_token,
        "pairing_code": code,
        "expires_at": expires_local,
        "delivery_dir": str(out_dir),
        "extension_zip": str(zip_path),
        "install_md": str(install_path),
        "server_url": SERVER_URL,
    }

    print()
    print("# === bb-onboard summary ===")
    for k, v in summary.items():
        print(f"# {k}: {v}")
    print()
    print(f"DELIVERY_DIR={out_dir}")
    print(f"PAIRING_CODE={code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
