#!/usr/bin/env python3
"""SQLite backup for danmi-browser-bridge.

Uses sqlite3's online backup API (``conn.backup(...)``) — safe to run while the
server is alive, no need to lock the DB or stop the service.

Outputs to ``data{,-dev}/backups/YYYY-MM-DD.db``. Keeps the most recent 14
days; older files are deleted. Same-day reruns overwrite (atomic via temp +
os.replace).

Usage::

    ./scripts/backup_db.py             # prod (data/browser_bridge.db)
    ./scripts/backup_db.py --env dev   # dev  (data-dev/browser_bridge.db)
    ./scripts/backup_db.py --env prod  # explicit

Cron deployment: see deploy/cron/danmi-bb-backup.cron.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# How many daily backups to keep.
RETENTION_DAYS = 14


def _db_paths(env: str) -> tuple[Path, Path]:
    """(source DB, backup directory) for the given env."""
    if env == "prod":
        src = REPO_ROOT / "data" / "browser_bridge.db"
        dst_dir = REPO_ROOT / "data" / "backups"
    elif env == "dev":
        src = REPO_ROOT / "data-dev" / "browser_bridge.db"
        dst_dir = REPO_ROOT / "data-dev" / "backups"
    else:
        raise SystemExit(f"unknown env: {env!r} (expected prod|dev)")
    return src, dst_dir


def _online_backup(src: Path, dst_tmp: Path) -> None:
    """Use sqlite3.backup(...) to copy src → dst_tmp atomically."""
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst_tmp))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _prune_old(dst_dir: Path, keep_days: int) -> list[Path]:
    """Delete YYYY-MM-DD.db files whose date is older than keep_days. Return removed paths."""
    cutoff = datetime.now().date() - timedelta(days=keep_days - 1)
    removed: list[Path] = []
    for f in dst_dir.glob("*.db"):
        stem = f.stem  # YYYY-MM-DD
        try:
            d = datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            continue  # not a daily backup; leave alone
        if d < cutoff:
            f.unlink()
            removed.append(f)
    return removed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    p.add_argument(
        "--env",
        default=os.environ.get("BB_ENV", "prod"),
        choices=["prod", "dev"],
        help="prod (default) or dev — determines source DB and backup dir",
    )
    p.add_argument(
        "--keep-days",
        type=int,
        default=RETENTION_DAYS,
        help=f"retention in days (default {RETENTION_DAYS})",
    )
    p.add_argument("--quiet", action="store_true", help="only print on error")
    args = p.parse_args()

    src, dst_dir = _db_paths(args.env)
    if not src.exists():
        print(f"source DB not found: {src}", file=sys.stderr)
        return 2

    dst_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    final_path = dst_dir / f"{today}.db"
    tmp_path = dst_dir / f".{today}.db.tmp"

    if tmp_path.exists():
        tmp_path.unlink()

    _online_backup(src, tmp_path)
    os.replace(tmp_path, final_path)

    removed = _prune_old(dst_dir, args.keep_days)

    if not args.quiet:
        size_mb = final_path.stat().st_size / (1024 * 1024)
        print(
            f"[bb-backup] env={args.env} src={src} → {final_path} "
            f"({size_mb:.2f} MB), pruned {len(removed)} old file(s)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
