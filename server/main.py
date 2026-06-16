"""Browser Bridge server entry point with startup banner.

Handles SIGTERM/SIGINT by first broadcasting ``shutdown_notice`` to every
connected device (giving them up to 5 seconds to react) and then signalling
uvicorn to exit. This is necessary because uvicorn's default shutdown order
closes connections *before* invoking the ASGI lifespan shutdown hook, which
would otherwise mean our notice never reaches anyone.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress

import uvicorn

from server.app import create_app_from_config
from server.config import load_config
from server.logging import get_logger, setup_logging


async def _async_main() -> None:
    config = load_config()
    log = get_logger("server.main")

    log.info(
        "browser_bridge_starting",
        host=config.server.host,
        port=config.server.port,
        config_path="config.toml",
        db_path=config.database.path,
    )

    app = await create_app_from_config()

    uvi_config = uvicorn.Config(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level="info",
    )
    server = uvicorn.Server(uvi_config)

    # Disable uvicorn's default signal install — it traps SIGTERM/SIGINT and
    # immediately closes connections. We want to broadcast first.
    server.install_signal_handlers = lambda: None

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    async def _graceful() -> None:
        log.info("graceful_shutdown_requested")
        gs = getattr(app.state, "graceful_shutdown", None)
        if gs is not None:
            with suppress(Exception):
                await gs(grace_seconds=5.0)
        server.should_exit = True
        shutdown_event.set()

    def _on_signal(_sig: int) -> None:
        if shutdown_event.is_set():
            # Second signal → force exit immediately.
            server.force_exit = True
            return
        asyncio.ensure_future(_graceful())

    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _on_signal, sig)

    await server.serve()


def main() -> None:
    import os
    setup_logging(debug=os.environ.get("BB_DEBUG", "0") == "1")
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
