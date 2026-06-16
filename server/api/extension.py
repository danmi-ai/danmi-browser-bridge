"""Extension version / latest-release endpoint.

Lets the extension auto-detect new releases (#8B) and lets the server enforce
a minimum required version (#16).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi import APIRouter, Request

from server.config import AppConfig
from server.logging import get_logger

log = get_logger("server.extension")

router = APIRouter()

_config: AppConfig | None = None
_zip_sha256: str | None = None
_zip_size: int | None = None


def _public_base(request: Request) -> str:
    """Best-effort public base URL for this server.

    Honours BB_PUBLIC_URL if set (e.g. behind a proxy / fixed deployment URL);
    otherwise derives from the incoming request so we never hard-code an IP.
    """
    override = os.environ.get("BB_PUBLIC_URL")
    if override:
        return override.rstrip("/")
    return str(request.base_url).rstrip("/")



def _compute_sha256(path: Path) -> tuple[str | None, int | None]:
    if not path.is_file():
        return None, None
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def init_extension_router(config: AppConfig) -> APIRouter:
    """Compute the bundled-zip sha256 once at startup, register routes."""
    global _config, _zip_sha256, _zip_size
    _config = config

    zip_path = os.environ.get("BB_EXTENSION_ZIP_PATH") or config.extension.zip_path
    if zip_path:
        # Resolve relative to current working directory (where the server runs).
        p = Path(zip_path).expanduser()
        sha, size = _compute_sha256(p)
        if sha is None:
            log.warning(
                "extension_zip_not_found",
                zip_path=str(p.resolve() if p.exists() else p),
            )
        else:
            log.info(
                "extension_zip_loaded",
                zip_path=str(p.resolve()),
                sha256=sha,
                size=size,
            )
        _zip_sha256 = sha
        _zip_size = size

    return router


@router.get("/extension/latest")
async def get_latest_extension(request: Request) -> dict:
    """Return the current published extension version + integrity hash.

    The extension polls this (on connect, on a timer, and on the popup's
    "检查更新" button) to surface a "new version available" banner; the server
    also force-disconnects any client whose reported version is below
    ``min_version``.

    URLs are derived from the incoming request (or BB_PUBLIC_URL) so we never
    bake in a fixed IP — a relocated deployment keeps working untouched.
    """
    assert _config is not None
    base = _public_base(request)
    return {
        "version": _config.extension.current_version,
        "min_version": _config.extension.min_version,
        "sha256": _zip_sha256,
        "size": _zip_size,
        "download_url": f"{base}/static/danmi-browser-bridge-extension.zip",
        "install_command": f"curl -sL {base}/static/install.sh | bash",
    }
