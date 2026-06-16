"""danmi-browser-bridge SDK · v0.8.0

Command API client. Use CommandClient for synchronous or AsyncCommandClient for async.

Example::

    from danmi_bridge import CommandClient
    # explicit server, or "auto" to self-discover via the BOS manifest:
    client = CommandClient("auto", token="bb_usr_...")
    client.navigate("https://example.com")
    snap = client.snapshot()
    client.close()
"""

from danmi_bridge.command_client import (
    AsyncCommandClient,
    CommandClient,
    CommandError,
    resolve_server,
)

__all__ = ["CommandClient", "AsyncCommandClient", "CommandError", "resolve_server"]
