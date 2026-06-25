#!/usr/bin/env python3
"""danmi-browser-bridge control script.

Subcommands:
  status            — process + port + health JSON
  start             — nohup uvicorn server.main; pid -> logs/server.pid
  stop              — SIGTERM the pid; server broadcasts shutdown_notice
  restart           — stop && start
  health            — curl /api/v1/health
  list-devices      — read DB devices (id, user_id, name, last_seen_at)
  list-users        — read DB users (id, name, created_at)
  list-sessions     — read DB sessions (active only)
  logs [N]          — tail N lines of logs/server.log (default 80)

All paths assume the canonical repo dir; override via BB_ROOT env var.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

import urllib.error
import urllib.request
from pathlib import Path

# Add parent to path so server.cli_fmt is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import cli as server_cli  # noqa: E402
from server.cli_fmt import should_use_json  # noqa: E402

# Repo lives in the skill's own directory now (since the migration to a unified
# skill-as-repo layout). Override via BB_ROOT if you really need it elsewhere.
_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = str(_SCRIPT_DIR.parent)
ROOT = Path(os.environ.get("BB_ROOT", DEFAULT_ROOT)).resolve()

# --- Environment & config selection ----------------------------------------
# BB_ENV: "prod" (default) or "dev". Used to derive default config + paths
#   so dev never collides with prod (different port / db / pid / log).
# BB_CONFIG: explicit override of the config TOML to load (relative to ROOT or
#   absolute). Falls back to config.toml (prod) or config.dev.toml (dev).
BB_ENV = os.environ.get("BB_ENV", "prod").strip().lower() or "prod"
if BB_ENV not in {"prod", "dev"}:
    print(f"warning: BB_ENV={BB_ENV!r} unrecognised — treating as 'prod'", file=sys.stderr)
    BB_ENV = "prod"

_DEFAULT_CONFIG_NAME = "config.dev.toml" if BB_ENV == "dev" else "config.toml"
_CONFIG_PATH_RAW = os.environ.get("BB_CONFIG", _DEFAULT_CONFIG_NAME)
CONFIG_PATH = Path(_CONFIG_PATH_RAW)
if not CONFIG_PATH.is_absolute():
    CONFIG_PATH = (ROOT / CONFIG_PATH).resolve()


def _load_port_from_config(default: int) -> int:
    """Read [server].port from CONFIG_PATH, fall back to default if absent."""
    if not CONFIG_PATH.exists():
        return default
    try:
        with open(CONFIG_PATH, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return default
    return int(raw.get("server", {}).get("port", default))


PORT = _load_port_from_config(8413 if BB_ENV == "dev" else 8404)
HEALTH_URL = f"http://127.0.0.1:{PORT}/api/v1/health"

# Per-env path suffix so dev/prod don't share PID / log / DB.
# Prod keeps legacy paths for backward-compat. Dev gets explicit "-dev" suffix.
if BB_ENV == "dev":
    LOG_FILE = ROOT / "logs" / "server-dev.log"
    PID_FILE = ROOT / "logs" / "server-dev.pid"
    DB_FILE = ROOT / "data-dev" / "browser_bridge.db"
else:
    LOG_FILE = ROOT / "logs" / "server.log"
    PID_FILE = ROOT / "logs" / "server.pid"
    DB_FILE = ROOT / "data" / "browser_bridge.db"

# Server side wants /usr/bin/python3.11 (where structlog/aiosqlite/fastapi are installed).
# `sys.executable` is wrong when ctl.py itself is invoked via the vot python.
SERVER_PYTHON = os.environ.get("BB_SERVER_PYTHON", str(ROOT / ".venv" / "bin" / "python"))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if _pid_alive(pid) else None


def _port_listening() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


def _curl_health(timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def cmd_status(_args) -> int:
    pid = _read_pid()
    listening = _port_listening()
    health = _curl_health()
    out = {
        "env": BB_ENV,
        "config": str(CONFIG_PATH),
        "root": str(ROOT),
        "pid": pid,
        "pid_file": str(PID_FILE),
        "port": PORT,
        "listening": listening,
        "health": health,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    healthy = bool(pid and listening and health and health.get("status") == "ok")
    return 0 if healthy else 2


def cmd_health(_args) -> int:
    h = _curl_health(timeout=3.0)
    if h is None:
        print("health: UNREACHABLE", file=sys.stderr)
        return 2
    print(json.dumps(h, indent=2, ensure_ascii=False))
    return 0


def cmd_start(_args) -> int:
    pid = _read_pid()
    if pid and _port_listening():
        print(f"already running pid={pid} port={PORT}", file=sys.stderr)
        return 0
    if pid and not _port_listening():
        print(f"pid {pid} alive but port not listening — kill first", file=sys.stderr)
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
        except OSError:
            pass

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_FILE, "ab")
    # Pass BB_CONFIG / BB_ENV down to the server subprocess so server.config
    # loads the right TOML. (server.main reads load_config() at startup.)
    child_env = os.environ.copy()
    child_env["BB_ENV"] = BB_ENV
    child_env["BB_CONFIG"] = str(CONFIG_PATH)
    proc = subprocess.Popen(
        [SERVER_PYTHON, "-m", "server.main"],
        cwd=str(ROOT),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=child_env,
    )
    PID_FILE.write_text(str(proc.pid))

    # wait for port up to 10s
    for _ in range(20):
        if _port_listening():
            print(f"started pid={proc.pid} port={PORT}")
            _publish_discovery()
            return 0
        time.sleep(0.5)
    print(f"started pid={proc.pid} but port not listening within 10s — check logs", file=sys.stderr)
    return 2


def _publish_discovery() -> None:
    """Announce this server's address to a discovery anchor so clients self-discover it.

    Best-effort and optional: the publish script ships only in the internal
    overlay (internal/scripts/). When it's absent (open-source checkout), this
    is a no-op and never blocks server startup.
    """
    script = ROOT / "internal" / "scripts" / "publish_discovery.py"
    if not script.is_file():
        return
    try:
        r = subprocess.run(
            [SERVER_PYTHON, str(script), "--server-only"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
            env={**os.environ, "BB_CONFIG": str(CONFIG_PATH)},
        )
        if r.returncode == 0:
            print("discovery published", file=sys.stderr)
        else:
            print(f"warning: discovery publish failed (rc={r.returncode}): "
                  f"{(r.stderr or '').strip()[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"warning: discovery publish skipped: {e}", file=sys.stderr)



def cmd_stop(_args) -> int:
    pid = _read_pid()
    if not pid:
        print("not running", file=sys.stderr)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"kill failed: {e}", file=sys.stderr)
        return 1
    for _ in range(20):
        if not _pid_alive(pid):
            try:
                PID_FILE.unlink()
            except OSError:
                pass
            print(f"stopped pid={pid}")
            return 0
        time.sleep(0.5)
    print(f"pid {pid} did not exit in 10s; sending SIGKILL", file=sys.stderr)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return 2


def cmd_restart(args) -> int:
    rc = cmd_stop(args)
    # Stop returns 2 if it had to SIGKILL but the process is gone now;
    # that's fine for a restart.
    if rc not in (0, 2):
        return rc
    return cmd_start(args)


def cmd_list_devices(_args) -> int:
    asyncio.run(server_cli._list_devices(None, active_only=False, as_json=should_use_json(_args)))
    return 0


def cmd_list_users(_args) -> int:
    asyncio.run(server_cli._list_users(active_only=False, as_json=should_use_json(_args)))
    return 0


def cmd_list_sessions(_args) -> int:
    asyncio.run(server_cli._list_sessions(active_only=True, as_json=should_use_json(_args)))
    return 0


def cmd_logs(args) -> int:
    n = args.lines
    if not LOG_FILE.exists():
        print(f"no log: {LOG_FILE}", file=sys.stderr)
        return 1
    out = subprocess.run(["tail", f"-n{n}", str(LOG_FILE)], check=False)
    return out.returncode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bb-ctl", description=__doc__)
    p.add_argument("--json", action="store_true", help="Force JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="process+port+health")
    sub.add_parser("start", help="start server")
    sub.add_parser("stop", help="SIGTERM server")
    sub.add_parser("restart", help="stop+start")
    sub.add_parser("health", help="curl /api/v1/health")
    sub.add_parser("list-devices", help="DB devices")
    sub.add_parser("list-users", help="DB users")
    sub.add_parser("list-sessions", help="DB active sessions")
    plogs = sub.add_parser("logs", help="tail server.log")
    plogs.add_argument("lines", nargs="?", type=int, default=80)

    args = p.parse_args(argv)
    handler = {
        "status": cmd_status,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "health": cmd_health,
        "list-devices": cmd_list_devices,
        "list-users": cmd_list_users,
        "list-sessions": cmd_list_sessions,
        "logs": cmd_logs,
    }[args.cmd]
    return handler(args) or 0


if __name__ == "__main__":
    sys.exit(main())
