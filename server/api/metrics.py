"""Prometheus metrics endpoint — GET /api/v1/metrics.

Internal monitoring; requires the admin token (X-Admin-Token). Exposes:

  bb_connected_devices         (gauge) WS-connected extension count
  bb_active_sessions           (gauge) sessions in state='active'
  bb_commands_total{cmd,status} (counter) cumulative command outcomes
  bb_command_duration_seconds  (histogram) per-command wall-clock latency
  bb_uptime_seconds            (gauge) since process start

The counter / histogram are populated by callers invoking ``observe_command``
after each finished command. Gauges are read live from the connection
manager and the DB whenever the metrics endpoint is scraped.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Header, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from server.storage.database import Database
from server.ws.connection_manager import ConnectionManager

router = APIRouter()

# Use a dedicated registry so prometheus_client's default process collector
# (sys, gc, etc.) doesn't pollute our scrape with unrelated default series.
# Add it back later if we ever want it.
REGISTRY = CollectorRegistry()

bb_uptime_seconds = Gauge(
    "bb_uptime_seconds",
    "Seconds since the bridge server process started.",
    registry=REGISTRY,
)
bb_connected_devices = Gauge(
    "bb_connected_devices",
    "Number of currently WS-connected browser extensions.",
    registry=REGISTRY,
)
bb_active_sessions = Gauge(
    "bb_active_sessions",
    "Number of sessions in state='active'.",
    registry=REGISTRY,
)
bb_commands_total = Counter(
    "bb_commands_total",
    "Cumulative count of commands by name and outcome.",
    ["cmd", "status"],
    registry=REGISTRY,
)
bb_command_duration_seconds = Histogram(
    "bb_command_duration_seconds",
    "Wall-clock latency of POST /sessions/{sid}/commands handler (seconds).",
    ["cmd"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
    registry=REGISTRY,
)

_db: Database | None = None
_cm: ConnectionManager | None = None
_start_time: float = time.time()


def init_metrics_router(db: Database, connection_manager: ConnectionManager) -> APIRouter:
    global _db, _cm
    _db = db
    _cm = connection_manager
    return router


def observe_command(cmd: str, status: str, duration_seconds: float) -> None:
    """Record a finished command for the counter and histogram.

    Called after each command returns (whether it
    completed, failed, timed out, or got rate-limited). ``status`` is one of:
    completed / failed / timeout / rate_limited / denied.
    """
    try:
        bb_commands_total.labels(cmd=cmd, status=status).inc()
        bb_command_duration_seconds.labels(cmd=cmd).observe(max(0.0, duration_seconds))
    except Exception:
        # Never let metrics breakage break a real command.
        pass


@router.get("/metrics")
async def metrics_endpoint(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    """Prometheus text format scrape endpoint."""
    from server.api.admin import _check_admin

    _check_admin(x_admin_token)
    # Refresh gauges on every scrape.
    bb_uptime_seconds.set(time.time() - _start_time)
    if _cm is not None:
        bb_connected_devices.set(_cm.connected_count)
    if _db is not None:
        try:
            row = await _db.fetchone(
                "SELECT COUNT(*) as cnt FROM sessions WHERE state = 'active'"
            )
            bb_active_sessions.set(int(row["cnt"]) if row else 0)
        except Exception:
            pass

    payload = generate_latest(REGISTRY)
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)
