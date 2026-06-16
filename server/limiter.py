"""Rate-limit and concurrency gate for the command pipeline (#14).

Three layers:
  1. per-session sliding window  (default: 100 cmds / 60 s)
  2. per-device concurrent cap   (default: 5 in-flight)
  3. per-user sliding window     (default: 1000 cmds / 86400 s)

All windows use ``deque[float]`` of timestamps in seconds. The implementation
is intentionally in-memory; restarting the server resets counters (matches
how the rest of the bridge handles state).

Triggered limits raise :class:`RateLimitedError` so the API layer can
translate to a 429 with structured body.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock

from server.config import LimitsConfig


@dataclass
class RateLimitedError(Exception):
    code: str = "RATE_LIMITED"
    level: str = "session"
    retry_after_s: float = 1.0
    message: str = ""

    def __post_init__(self) -> None:  # pragma: no cover - cosmetic
        super().__init__(self.message or f"rate_limited:{self.level}")


class RateLimiter:
    def __init__(self, limits: LimitsConfig):
        self._limits = limits
        self._lock = Lock()
        self._sessions: dict[str, deque[float]] = defaultdict(deque)
        self._users: dict[str, deque[float]] = defaultdict(deque)
        self._device_inflight: dict[str, int] = defaultdict(int)

    def _evict(self, dq: deque[float], window_s: float, now: float) -> None:
        cutoff = now - window_s
        while dq and dq[0] < cutoff:
            dq.popleft()

    def check_and_acquire(
        self, *, session_id: str, device_id: str, user_id: str
    ) -> None:
        """Atomically check all three limits and (if all pass) record the
        admission. Raises :class:`RateLimitedError` on the first miss.

        Caller MUST pair this with :meth:`release` (concurrent counter) when
        the command resolves.
        """
        now = time.time()
        L = self._limits
        with self._lock:
            # ---- session window ----
            dq = self._sessions[session_id]
            self._evict(dq, L.per_session_window_seconds, now)
            if len(dq) >= L.per_session_max:
                # When the window is full, the soonest a slot frees up is
                # when the oldest entry ages out.
                retry = max(1.0, L.per_session_window_seconds - (now - dq[0]))
                raise RateLimitedError(
                    level="session",
                    retry_after_s=retry,
                    message=(
                        f"session rate limit exceeded "
                        f"({L.per_session_max} / {L.per_session_window_seconds}s)"
                    ),
                )

            # ---- user window ----
            dq_u = self._users[user_id]
            self._evict(dq_u, L.per_user_window_seconds, now)
            if len(dq_u) >= L.per_user_max:
                retry = max(1.0, L.per_user_window_seconds - (now - dq_u[0]))
                raise RateLimitedError(
                    level="user",
                    retry_after_s=retry,
                    message=(
                        f"user rate limit exceeded "
                        f"({L.per_user_max} / {L.per_user_window_seconds}s)"
                    ),
                )

            # ---- device concurrency ----
            inflight = self._device_inflight.get(device_id, 0)
            if inflight >= L.per_device_concurrent_max:
                raise RateLimitedError(
                    level="device",
                    retry_after_s=1.0,
                    message=(
                        f"device concurrency limit exceeded "
                        f"({L.per_device_concurrent_max} in-flight)"
                    ),
                )

            # admit
            dq.append(now)
            dq_u.append(now)
            self._device_inflight[device_id] = inflight + 1

    def release(self, *, device_id: str) -> None:
        """Decrement the device's in-flight counter. Idempotent if called
        from a defensive ``finally`` block.
        """
        with self._lock:
            cur = self._device_inflight.get(device_id, 0)
            if cur > 0:
                self._device_inflight[device_id] = cur - 1

    def snapshot(self) -> dict:
        """Return a read-only snapshot of current rate-limit state."""
        with self._lock:
            now = time.time()
            L = self._limits
            sessions = {}
            for sid, dq in self._sessions.items():
                self._evict(dq, L.per_session_window_seconds, now)
                if dq:
                    sessions[sid] = {"count": len(dq), "max": L.per_session_max}
            users = {}
            for uid, dq in self._users.items():
                self._evict(dq, L.per_user_window_seconds, now)
                if dq:
                    users[uid] = {"count": len(dq), "max": L.per_user_max}
            devices = {
                did: {"inflight": n, "max": L.per_device_concurrent_max}
                for did, n in self._device_inflight.items() if n > 0
            }
            return {"sessions": sessions, "users": users, "devices": devices}

    @contextmanager
    def admit(self, *, session_id: str, device_id: str, user_id: str):
        """Context manager wrapper: acquire on enter, release on exit."""
        self.check_and_acquire(
            session_id=session_id, device_id=device_id, user_id=user_id
        )
        try:
            yield
        finally:
            self.release(device_id=device_id)
