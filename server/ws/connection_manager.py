"""WebSocket connection manager — tracks device_id to websocket mapping."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from fastapi import WebSocket


class DeviceOfflineError(Exception):
    pass


class CommandError(Exception):
    def __init__(self, response: dict):
        self.response = response
        super().__init__(str(response))


@dataclass
class DeviceConnection:
    """Active WebSocket connection for a device."""

    device_id: str
    user_id: str
    device_name: str
    websocket: WebSocket
    connected_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    # Runtime mirror of the extension's paused state. The extension's
    # storage.local is the source of truth; this is a per-connection cache
    # synced via the "extension.paused_changed" frame (and re-synced on every
    # reconnect). Used only as a fast pre-send gate — the extension enforces
    # authoritatively. Defaults False; a fresh reconnect is "not paused" until
    # the client tells us otherwise.
    paused: bool = False


class ConnectionManager:
    """Track active device WebSocket connections."""

    def __init__(self):
        self._connections: dict[str, DeviceConnection] = {}
        self._lock = asyncio.Lock()
        # msg_id -> (owning device_id, future). The device_id binding prevents
        # a malicious device from resolving another device's pending command
        # (result-forgery): resolve_command only fires if the resolving socket's
        # own device_id matches the owner stored here.
        self._pending_commands: dict[int, tuple[str, asyncio.Future]] = {}
        self._next_msg_id: int = 1

    async def connect(
        self, device_id: str, user_id: str, device_name: str, websocket: WebSocket
    ) -> DeviceConnection:
        """Register a new device connection. Disconnects existing if duplicate."""
        async with self._lock:
            if device_id in self._connections:
                old = self._connections[device_id]
                try:
                    await old.websocket.close(code=4003, reason="replaced")
                except Exception:
                    pass

            conn = DeviceConnection(
                device_id=device_id,
                user_id=user_id,
                device_name=device_name,
                websocket=websocket,
            )
            self._connections[device_id] = conn
            return conn

    async def disconnect(
        self, device_id: str, conn: DeviceConnection | None = None
    ) -> None:
        """Remove a device connection.

        Identity-aware: when ``conn`` is provided, only pop if the currently
        stored connection *is* that object (compare-and-delete). This prevents
        a stale handler's teardown from evicting a NEWER connection that a
        reconnect already installed under the same device_id. When ``conn`` is
        None, pop unconditionally by key.
        """
        async with self._lock:
            if conn is None or self._connections.get(device_id) is conn:
                self._connections.pop(device_id, None)

    def get(self, device_id: str) -> DeviceConnection | None:
        """Get connection for a device, or None if not connected."""
        return self._connections.get(device_id)

    def is_connected(self, device_id: str) -> bool:
        return device_id in self._connections

    def update_heartbeat(self, device_id: str) -> None:
        """Update last heartbeat timestamp for a device."""
        conn = self._connections.get(device_id)
        if conn:
            conn.last_heartbeat = time.time()

    def set_paused(self, device_id: str, paused: bool) -> None:
        """Update the runtime paused mirror for a device (no-op if offline)."""
        conn = self._connections.get(device_id)
        if conn:
            conn.paused = paused

    @property
    def connected_count(self) -> int:
        return len(self._connections)

    @property
    def connected_device_ids(self) -> list[str]:
        return list(self._connections.keys())

    async def revoke(self, device_id: str) -> bool:
        """Forcibly close a device's WebSocket with 4002 (revoked).

        Returns True if the device was connected (and now closed), False if
        it wasn't currently connected.
        """
        async with self._lock:
            conn = self._connections.pop(device_id, None)
        if conn is None:
            return False
        try:
            await conn.websocket.close(code=4002, reason="device_revoked")
        except Exception:
            pass
        return True

    async def broadcast_shutdown(self, grace_seconds: float = 5.0) -> int:
        """Notify all connected devices the server is shutting down.

        Sends a JSON ``shutdown_notice`` frame to every device, waits up to
        ``grace_seconds`` for them to react, then closes any still-open sockets
        with 1001 (going away).

        Returns the number of devices that were notified.
        """
        async with self._lock:
            conns = list(self._connections.values())
            self._connections.clear()

        if not conns:
            return 0

        notice = json.dumps({"type": "shutdown_notice", "grace_seconds": grace_seconds})
        for conn in conns:
            try:
                await conn.websocket.send_text(notice)
            except Exception:
                pass

        await asyncio.sleep(grace_seconds)

        for conn in conns:
            try:
                await conn.websocket.close(code=1001, reason="server_shutdown")
            except Exception:
                pass

        return len(conns)

    async def send_and_wait(self, device_id: str, msg: dict, timeout: float) -> dict:
        """Send a command message to a device and wait for its response.

        Raises DeviceOfflineError if the device is not connected.
        Raises CommandError if the device returns an error response.
        Raises asyncio.TimeoutError if no response within timeout seconds.
        """
        conn = self._connections.get(device_id)
        if conn is None:
            raise DeviceOfflineError(f"Device {device_id} is not connected")

        msg_id = self._next_msg_id
        self._next_msg_id += 1
        msg["msg_id"] = msg_id

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_commands[msg_id] = (device_id, future)

        try:
            await conn.websocket.send_text(json.dumps(msg))
            result = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_commands.pop(msg_id, None)

        if result.get("type") == "error":
            raise CommandError(result)

        return result.get("data", result)

    def resolve_command(self, device_id: str, msg_id: int, response: dict) -> bool:
        """Resolve a pending command future with the given response.

        The resolving ``device_id`` must own the pending command — otherwise a
        malicious device could forge results for another device's command. Only
        resolves if the stored owner matches the resolving device.

        Returns True if a pending future was resolved, False otherwise.
        """
        entry = self._pending_commands.get(msg_id)
        if entry is None:
            return False
        owner_id, future = entry
        if owner_id != device_id or future.done():
            return False
        future.set_result(response)
        return True
