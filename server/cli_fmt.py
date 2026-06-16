"""Dual-mode CLI output: JSON for pipes/agents, colored tables for humans."""

from __future__ import annotations

import json
import sys
from typing import Any

_COLORS = {
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "gray": "\033[90m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def _use_color() -> bool:
    return sys.stdout.isatty()


def c(text: str, color: str) -> str:
    if not _use_color() or color not in _COLORS:
        return text
    return f"{_COLORS[color]}{text}{_COLORS['reset']}"


def status_color(value: str) -> str:
    v = str(value).lower()
    if v in ("active", "online", "true", "1", "yes", "ok"):
        return c(value, "green")
    if v in ("paused",):
        return c(value, "yellow")
    if v in ("revoked", "offline", "false", "0", "no", "inactive", "closed"):
        return c(value, "gray")
    if v in ("failed", "expired", "timeout", "error"):
        return c(value, "red")
    return value


def should_use_json(args: Any = None) -> bool:
    if args and getattr(args, "json", False):
        return True
    return not sys.stdout.isatty()


def print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def render_table(
    rows: list[dict[str, Any]],
    columns: list[tuple[str, str]],
    title: str | None = None,
) -> None:
    """Render rows as an aligned table with optional title.

    columns: list of (key, header_label) tuples.
    """
    if not rows:
        if title:
            print(c(title, "bold"))
        print("  (empty)")
        return

    headers = [label for _, label in columns]
    keys = [key for key, _ in columns]

    col_widths = [len(h) for h in headers]
    str_rows: list[list[str]] = []
    for row in rows:
        cells = []
        for i, key in enumerate(keys):
            val = str(row.get(key, ""))
            cells.append(val)
            col_widths[i] = max(col_widths[i], len(val))
        str_rows.append(cells)

    if title:
        print(c(title, "bold"))

    header_line = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    print(c(header_line, "bold"))
    print("  ".join("─" * w for w in col_widths))

    for cells in str_rows:
        colored = []
        for i, cell in enumerate(cells):
            key = keys[i]
            if key in (
                "is_active", "state", "status", "online",
                "paused", "evaluate_enabled", "network_enabled",
            ):
                colored.append(
                    status_color(cell).ljust(
                        col_widths[i] + (len(status_color(cell)) - len(cell))
                    )
                )
            else:
                colored.append(cell.ljust(col_widths[i]))
        print("  ".join(colored))
