"""FastAPI application factory for browser-bridge server."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from server.api.admin import init_admin_router
from server.api.audit import init_audit_router
from server.api.audit_user import init_audit_user_router
from server.api.command import init_command_router
from server.api.devices import init_devices_router
from server.api.extension import init_extension_router
from server.api.health import init_health_router
from server.api.metrics import init_metrics_router
from server.api.onboard import init_onboard_router
from server.api.pairing import init_pairing_router
from server.api.popup_config import init_popup_config_router
from server.api.sessions import init_sessions_router
from server.audit.logger import AuditLogger
from server.auth.dependencies import init_auth_dependency
from server.config import AppConfig, load_config
from server.limiter import RateLimiter
from server.logging import get_logger, setup_logging
from server.sessions.manager import SessionManager
from server.storage.database import Database
from server.storage.migrations import apply_migrations
from server.ws.connection_manager import ConnectionManager
from server.ws.handler import init_ws_router


def create_app(
    config: AppConfig, db: Database, connection_manager: ConnectionManager | None = None
) -> FastAPI:
    if connection_manager is None:
        connection_manager = ConnectionManager()

    init_auth_dependency(db)

    session_manager = SessionManager(db, config, connection_manager)
    audit_logger = AuditLogger(db)
    rate_limiter = RateLimiter(config.limits)
    log = get_logger("server.app")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await session_manager.start()
        revocation_task = asyncio.create_task(
            _revocation_watcher(db, connection_manager, log)
        )
        log.info(
            "server_ready",
            host=config.server.host,
            port=config.server.port,
            db_path=config.database.path,
        )
        yield
        log.info("server_shutting_down")
        revocation_task.cancel()
        try:
            await revocation_task
        except asyncio.CancelledError:
            pass
        # If graceful_shutdown was already invoked (e.g. via signal handler
        # or admin endpoint), broadcast_shutdown is a no-op the second time
        # because _connections is already empty.
        notified = await connection_manager.broadcast_shutdown(grace_seconds=5.0)
        log.info("shutdown_notice_sent", devices=notified)
        await session_manager.stop()

    app = FastAPI(title="Browser Bridge", version=config.server.version, lifespan=lifespan)

    app.state.connection_manager = connection_manager
    app.state.session_manager = session_manager
    app.state.audit_logger = audit_logger
    app.state.rate_limiter = rate_limiter

    async def graceful_shutdown(grace_seconds: float = 5.0) -> int:
        """Broadcast shutdown_notice to every connected device and wait for
        them to disconnect (or timeout). Call this *before* signalling uvicorn
        to exit so the notice goes out before sockets are torn down.
        """
        notified = await connection_manager.broadcast_shutdown(
            grace_seconds=grace_seconds
        )
        log.info("graceful_shutdown_broadcast", devices=notified)
        return notified

    app.state.graceful_shutdown = graceful_shutdown

    health_router = init_health_router(db, connection_manager, server_version=config.server.version)
    app.include_router(health_router, prefix="/api/v1")

    pairing_router = init_pairing_router(db, audit_logger)
    app.include_router(pairing_router, prefix="/api/v1")

    devices_router = init_devices_router(db)
    app.include_router(devices_router, prefix="/api/v1")

    ws_router = init_ws_router(db, config, connection_manager, audit_logger)
    app.include_router(ws_router, prefix="/api/v1")

    sessions_router = init_sessions_router(session_manager)
    app.include_router(sessions_router, prefix="/api/v1")

    audit_router = init_audit_router(audit_logger)
    app.include_router(audit_router, prefix="/api/v1")

    audit_user_router = init_audit_user_router(db, audit_logger)
    app.include_router(audit_user_router, prefix="/api/v1")

    extension_router = init_extension_router(config)
    app.include_router(extension_router, prefix="/api/v1")

    metrics_router = init_metrics_router(db, connection_manager)
    app.include_router(metrics_router, prefix="/api/v1")

    popup_config_router = init_popup_config_router()
    app.include_router(popup_config_router, prefix="/api/v1")

    command_router = init_command_router(db, connection_manager, rate_limiter)
    app.include_router(command_router, prefix="/api/v1")

    # admin token lives next to the DB so dev/prod naturally diverge.
    from pathlib import Path as _Path
    db_dir = _Path(config.database.path).resolve().parent
    admin_token_path = db_dir / ".admin_token"
    admin_router = init_admin_router(
        admin_token_path=admin_token_path,
        rate_limiter=rate_limiter,
        connection_manager=connection_manager,
        db=db,
        config=config,
    )
    app.include_router(admin_router, prefix="/api/v1")

    onboard_router = init_onboard_router(db, admin_token_path=admin_token_path)
    app.include_router(onboard_router, prefix="/api/v1")

    from pathlib import Path as _WebPath

    from fastapi.staticfiles import StaticFiles as _StaticFiles
    _web_dir = _WebPath(__file__).resolve().parent.parent / "web"
    if _web_dir.is_dir():
        # install.sh is now self-contained (server-independent): it reads the BOS
        # discovery anchor at runtime for the server URL, so no host injection is
        # needed — it's served as a plain static file from both server and BOS.
        app.mount("/static", _StaticFiles(directory=str(_web_dir)), name="static")

    return app


async def _revocation_watcher(
    db: Database, manager: ConnectionManager, log, interval: float = 30.0
) -> None:
    """Periodically scan connected devices and forcibly close any whose row
    in the ``devices`` table has ``is_active = 0`` (revoked by an admin).

    Closes affected sockets with WS code 4002 (device_revoked).
    """
    while True:
        try:
            await asyncio.sleep(interval)
            ids = manager.connected_device_ids
            if not ids:
                continue
            placeholders = ",".join("?" * len(ids))
            rows = await db.fetchall(
                f"SELECT id FROM devices WHERE id IN ({placeholders}) AND is_active = 0",
                tuple(ids),
            )
            revoked_ids = [r["id"] for r in rows]
            for dev_id in revoked_ids:
                closed = await manager.revoke(dev_id)
                if closed:
                    log.warning("device_revoked_disconnect", device_id=dev_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            log.warning("revocation_watcher_error", error=str(e))


async def create_app_from_config() -> FastAPI:
    """Factory for uvicorn — loads config, initializes DB, returns app."""
    setup_logging()
    config = load_config()
    db = Database(config.database.path)
    await db.initialize()
    await apply_migrations(db)
    return create_app(config, db)
