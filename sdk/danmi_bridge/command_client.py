"""Command API client for danmi-browser-bridge.

Provides a simple synchronous and async Python interface to the Command API
(POST /api/v1/command). No Playwright dependency required.

Example::

    from danmi_bridge.command_client import CommandClient

    client = CommandClient("http://your-server:8404", token="bb_usr_...")
    tree = client.snapshot()
    client.click(ref=tree["tree"][0]["ref"])
    client.fill(ref="@e5", value="hello world")
    client.close()
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# Service-discovery anchor: a static JSON ({"server_url": "..."}) that lets the
# client self-heal when the server's IP changes. Set BB_DISCOVERY_URL to your
# own hosted anchor (object storage / CDN / any static endpoint). When unset,
# server="auto" raises rather than guessing.
DISCOVERY_URL = os.environ.get("BB_DISCOVERY_URL", "")


def resolve_server(server: str) -> str:
    """Resolve a server URL. If ``server == "auto"``, fetch the discovery
    manifest named by ``BB_DISCOVERY_URL`` (with a cache-buster, since static
    hosts often set a long Expires header) and return its ``server_url``."""
    if server != "auto":
        return server.rstrip("/")
    if not DISCOVERY_URL:
        raise CommandError(
            "DISCOVERY_UNCONFIGURED",
            "server='auto' needs BB_DISCOVERY_URL set, or pass an explicit URL",
            500,
        )
    import time as _time
    r = httpx.get(
        DISCOVERY_URL,
        params={"t": int(_time.time() * 1000)},
        headers={"Cache-Control": "no-cache"},
        timeout=10.0,
    )
    r.raise_for_status()
    url = (r.json() or {}).get("server_url", "").rstrip("/")
    if not url:
        raise CommandError("DISCOVERY_EMPTY", "discovery.json has no server_url", 502)
    return url


class CommandError(Exception):
    """Raised when the server returns a non-2xx response."""

    def __init__(self, code: str, message: str, status: int = 500):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"[{code}] {message}")


class CommandClient:
    """Synchronous client for the Bridge Command API."""

    def __init__(
        self,
        server: str,
        token: str,
        *,
        device_id: str | None = None,
        session: str = "default",
        timeout: float = 30.0,
    ):
        self._server = resolve_server(server)
        self._token = token
        self._device_id = device_id
        self._session = session
        self._timeout = timeout
        self._client = httpx.Client(
            base_url=self._server,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def _cmd(self, action: str, args: dict | None = None, **kwargs) -> dict:
        body: dict[str, Any] = {
            "action": action,
            "args": args or {},
            "session": self._session,
        }
        if self._device_id:
            body["device_id"] = self._device_id

        timeout = kwargs.pop("timeout", None)
        r = self._client.post(
            "/api/v1/command",
            json=body,
            timeout=timeout or self._timeout,
        )

        if r.status_code >= 400:
            data = (
                r.json()
                if r.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            detail = data.get("detail", data)
            if isinstance(detail, dict):
                raise CommandError(
                    code=detail.get("code", "UNKNOWN"),
                    message=detail.get("error", r.text),
                    status=r.status_code,
                )
            raise CommandError(code="UNKNOWN", message=str(detail), status=r.status_code)

        return r.json().get("result", r.json())

    def navigate(self, url: str, *, new_tab: bool = False, group_title: str | None = None) -> dict:
        args: dict[str, Any] = {"url": url}
        if new_tab:
            args["newTab"] = True
        if group_title:
            args["group_title"] = group_title
        return self._cmd("navigate", args, timeout=60.0)

    def snapshot(self) -> dict:
        return self._cmd("snapshot", timeout=15.0)

    def click(self, *, ref: str | None = None, selector: str | None = None) -> dict:
        args: dict[str, Any] = {}
        if ref:
            args["ref"] = ref
        if selector:
            args["selector"] = selector
        return self._cmd("click", args)

    def fill(self, value: str, *, ref: str | None = None, selector: str | None = None) -> dict:
        args: dict[str, Any] = {"value": value}
        if ref:
            args["ref"] = ref
        if selector:
            args["selector"] = selector
        return self._cmd("fill", args)

    def screenshot(self, *, format: str = "png", quality: int | None = None,
                   selector: str | None = None, ref: str | None = None) -> dict:
        args: dict[str, Any] = {"format": format}
        if quality is not None:
            args["quality"] = quality
        if selector:
            args["selector"] = selector
        if ref:
            args["ref"] = ref
        return self._cmd("screenshot", args, timeout=10.0)

    def evaluate(self, code: str) -> dict:
        return self._cmd("evaluate", {"code": code}, timeout=30.0)

    def network(
        self, cmd: str, *, filter: str | None = None, request_id: str | None = None
    ) -> dict:
        args: dict[str, Any] = {"cmd": cmd}
        if filter:
            args["filter"] = filter
        if request_id:
            args["requestId"] = request_id
        return self._cmd("network", args, timeout=10.0)

    def upload(
        self, files: list[dict], *, ref: str | None = None, selector: str | None = None
    ) -> dict:
        args: dict[str, Any] = {"files": files}
        if ref:
            args["ref"] = ref
        if selector:
            args["selector"] = selector
        return self._cmd("upload", args, timeout=30.0)

    def save_as_pdf(self, *, paper_format: str = "a4", landscape: bool = False,
                    scale: float = 1.0, print_background: bool = True) -> dict:
        args: dict[str, Any] = {
            "paper_format": paper_format,
            "landscape": landscape,
            "scale": scale,
            "print_background": print_background,
        }
        return self._cmd("save_as_pdf", args, timeout=30.0)

    def list_tabs(self) -> dict:
        return self._cmd("list_tabs")

    def find_tab(self, url: str, *, active: bool | None = None) -> dict:
        args: dict[str, Any] = {"url": url}
        if active is not None:
            args["active"] = active
        return self._cmd("find_tab", args)

    def close_tab(self) -> dict:
        return self._cmd("close_tab")

    def close_session(self) -> dict:
        return self._cmd("close_session")

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class AsyncCommandClient:
    """Async client for the Bridge Command API."""

    def __init__(
        self,
        server: str,
        token: str,
        *,
        device_id: str | None = None,
        session: str = "default",
        timeout: float = 30.0,
    ):
        self._server = resolve_server(server)
        self._token = token
        self._device_id = device_id
        self._session = session
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self._server,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def _cmd(self, action: str, args: dict | None = None, **kwargs) -> dict:
        body: dict[str, Any] = {
            "action": action,
            "args": args or {},
            "session": self._session,
        }
        if self._device_id:
            body["device_id"] = self._device_id

        timeout = kwargs.pop("timeout", None)
        r = await self._client.post(
            "/api/v1/command",
            json=body,
            timeout=timeout or self._timeout,
        )

        if r.status_code >= 400:
            data = (
                r.json()
                if r.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            detail = data.get("detail", data)
            if isinstance(detail, dict):
                raise CommandError(
                    code=detail.get("code", "UNKNOWN"),
                    message=detail.get("error", r.text),
                    status=r.status_code,
                )
            raise CommandError(code="UNKNOWN", message=str(detail), status=r.status_code)

        return r.json().get("result", r.json())

    async def navigate(
        self, url: str, *, new_tab: bool = False, group_title: str | None = None
    ) -> dict:
        args: dict[str, Any] = {"url": url}
        if new_tab:
            args["newTab"] = True
        if group_title:
            args["group_title"] = group_title
        return await self._cmd("navigate", args, timeout=60.0)

    async def snapshot(self) -> dict:
        return await self._cmd("snapshot", timeout=15.0)

    async def click(self, *, ref: str | None = None, selector: str | None = None) -> dict:
        args: dict[str, Any] = {}
        if ref:
            args["ref"] = ref
        if selector:
            args["selector"] = selector
        return await self._cmd("click", args)

    async def fill(
        self, value: str, *, ref: str | None = None, selector: str | None = None
    ) -> dict:
        args: dict[str, Any] = {"value": value}
        if ref:
            args["ref"] = ref
        if selector:
            args["selector"] = selector
        return await self._cmd("fill", args)

    async def screenshot(self, *, format: str = "png", quality: int | None = None,
                         selector: str | None = None, ref: str | None = None) -> dict:
        args: dict[str, Any] = {"format": format}
        if quality is not None:
            args["quality"] = quality
        if selector:
            args["selector"] = selector
        if ref:
            args["ref"] = ref
        return await self._cmd("screenshot", args, timeout=10.0)

    async def evaluate(self, code: str) -> dict:
        return await self._cmd("evaluate", {"code": code}, timeout=30.0)

    async def network(
        self, cmd: str, *, filter: str | None = None, request_id: str | None = None
    ) -> dict:
        args: dict[str, Any] = {"cmd": cmd}
        if filter:
            args["filter"] = filter
        if request_id:
            args["requestId"] = request_id
        return await self._cmd("network", args, timeout=10.0)

    async def upload(
        self, files: list[dict], *, ref: str | None = None, selector: str | None = None
    ) -> dict:
        args: dict[str, Any] = {"files": files}
        if ref:
            args["ref"] = ref
        if selector:
            args["selector"] = selector
        return await self._cmd("upload", args, timeout=30.0)

    async def save_as_pdf(self, *, paper_format: str = "a4", landscape: bool = False,
                          scale: float = 1.0, print_background: bool = True) -> dict:
        args: dict[str, Any] = {
            "paper_format": paper_format,
            "landscape": landscape,
            "scale": scale,
            "print_background": print_background,
        }
        return await self._cmd("save_as_pdf", args, timeout=30.0)

    async def list_tabs(self) -> dict:
        return await self._cmd("list_tabs")

    async def find_tab(self, url: str, *, active: bool | None = None) -> dict:
        args: dict[str, Any] = {"url": url}
        if active is not None:
            args["active"] = active
        return await self._cmd("find_tab", args)

    async def close_tab(self) -> dict:
        return await self._cmd("close_tab")

    async def close_session(self) -> dict:
        return await self._cmd("close_session")

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
