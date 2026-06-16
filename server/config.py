"""Configuration loader for Browser Bridge server."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class TimeoutsConfig:
    ws_heartbeat_interval: int = 15
    ws_heartbeat_timeout: int = 5
    command_default: int = 30
    command_navigate: int = 30
    command_snapshot: int = 10
    command_screenshot: int = 5
    command_click: int = 10
    command_fill: int = 10


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8100
    version: str = "0.8.0"
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)


@dataclass
class SessionConfig:
    idle_timeout: int = 300
    max_lifetime: int = 7200
    max_per_device: int = 3


@dataclass
class DatabaseConfig:
    path: str = "data/browser_bridge.db"


@dataclass
class PairingConfig:
    code_length: int = 6
    code_expiry: int = 1800


@dataclass
class ExtensionConfig:
    current_version: str = "0.4.0"
    min_version: str = "0.2.0"
    zip_path: str = ""  # relative to server cwd; sha256 computed at startup if exists


@dataclass
class LimitsConfig:
    per_session_window_seconds: int = 60
    per_session_max: int = 100
    per_device_concurrent_max: int = 5
    per_user_window_seconds: int = 86400
    per_user_max: int = 1000


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    pairing: PairingConfig = field(default_factory=PairingConfig)
    extension: ExtensionConfig = field(default_factory=ExtensionConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load configuration from TOML file, falling back to defaults.

    Resolution order:
      1. Explicit ``config_path`` argument (if non-None).
      2. ``BB_CONFIG`` env var (e.g. ``config.dev.toml`` for the dev instance).
      3. ``config.toml`` in the current working directory (legacy default).
    """
    if config_path is None:
        config_path = os.environ.get("BB_CONFIG", "config.toml")
    path = Path(config_path)
    if not path.exists():
        return AppConfig()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    server_raw = raw.get("server", {})
    timeouts_raw = server_raw.pop("timeouts", {})

    return AppConfig(
        server=ServerConfig(
            host=server_raw.get("host", "0.0.0.0"),
            port=server_raw.get("port", 8100),
            version=server_raw.get("version", ServerConfig.version),
            timeouts=TimeoutsConfig(**{
                k: v for k, v in timeouts_raw.items() if hasattr(TimeoutsConfig, k)
            }),
        ),
        session=SessionConfig(**{
            k: v for k, v in raw.get("session", {}).items() if hasattr(SessionConfig, k)
        }),
        database=DatabaseConfig(**{
            k: v for k, v in raw.get("database", {}).items() if hasattr(DatabaseConfig, k)
        }),
        pairing=PairingConfig(**{
            k: v for k, v in raw.get("pairing", {}).items() if hasattr(PairingConfig, k)
        }),
        extension=ExtensionConfig(**{
            k: v for k, v in raw.get("extension", {}).items() if hasattr(ExtensionConfig, k)
        }),
        limits=LimitsConfig(**{
            k: v
            for k, v in raw.get("server", {}).get("limits", {}).items()
            if hasattr(LimitsConfig, k)
        }),
    )


def get_server_url(config: AppConfig) -> str:
    """Resolve the externally-reachable base URL of this server.

    Resolution order:
      1. ``BB_PUBLIC_URL`` env var — explicit public base (proxy / fixed deploy).
      2. ``BB_SERVER`` env var — already-resolved URL.
      3. Detected host IP + configured port. ``config.host`` is the bind address
         (often ``0.0.0.0``), which isn't dialable, so we detect the first
         non-loopback IPv4 instead. ``BB_SERVER_IP`` overrides detection.
    """
    for key in ("BB_PUBLIC_URL", "BB_SERVER"):
        val = os.environ.get(key)
        if val:
            return val.rstrip("/")
    port = config.server.port
    ip = os.environ.get("BB_SERVER_IP") or _detect_host_ip()
    return f"http://{ip}:{port}"


def _detect_host_ip() -> str:
    """First non-loopback IPv4, via a dummy UDP connect; falls back to localhost."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"
