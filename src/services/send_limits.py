"""Send limits: rate windows and the dedup window, reserved in one step.

``SendLimits.reserve`` checks and reserves without an ``await`` in
between, so two concurrent requests can't both pass (asyncio is
single-threaded). ``critical`` has its own hourly ceiling and no dedup.
"""
from __future__ import annotations

import hashlib
import time
from collections import deque
from collections.abc import Callable

from src.config import Settings
from src.models.schemas import Priority


class RateLimitExceeded(Exception):
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class DuplicateMessage(Exception):
    pass


class SlidingWindow:
    """Timestamps of sends within the last hour (monotonic clock)."""

    SPAN = 3600

    def __init__(self) -> None:
        self._ts: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._ts and self._ts[0] <= now - self.SPAN:
            self._ts.popleft()

    def check(self, now: float, limit: int, span: int, label: str) -> None:
        """Raise ``RateLimitExceeded`` if ``limit`` sends happened in the last ``span`` seconds."""
        self._prune(now)
        recent = [t for t in self._ts if t > now - span]
        if len(recent) >= limit:
            retry_after = max(1, int(recent[0] + span - now) + 1)
            raise RateLimitExceeded(f"Rate limit: {limit}/{label} exceeded", retry_after=retry_after)

    def add(self, now: float) -> None:
        self._ts.append(now)

    def usage(self, now: float, span: int) -> tuple[int, int]:
        """(sends in the last ``span`` s, seconds until the oldest of them leaves)."""
        self._prune(now)
        recent = [t for t in self._ts if t > now - span]
        frees_in = max(1, int(recent[0] + span - now) + 1) if recent else 0
        return len(recent), frees_in


class SendLimits:
    """Normal traffic and critical traffic have separate windows. Critical
    bypasses the normal limits but still has a hard hourly ceiling to
    protect the SIM credit."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._normal = SlidingWindow()
        self._critical = SlidingWindow()
        self._dedup: dict[str, float] = {}  # dedup key → expiry (monotonic)

    @staticmethod
    def dedup_key(number: str, message: str, alert_key: str | None = None) -> str:
        """Per-recipient dedup bucket: the producer's alert key if given,
        else the text (so "SOC 14.1 %" / "SOC 14.8 %" differ without a key)."""
        basis = f"key:{alert_key}" if alert_key else f"text:{message}"
        return hashlib.sha256(f"{number}|{basis}".encode()).hexdigest()[:16]

    def reserve(self, number: str, message: str, priority: Priority,
                alert_key: str | None = None,
                is_queued: Callable[[str], bool] | None = None) -> str | None:
        """Check rate limit + dedup and reserve the slot in one step.

        ``is_queued(key)`` says whether the outbox already holds that
        message. Returns the dedup key reserved (None for critical, which
        bypasses dedup). Raises ``RateLimitExceeded`` / ``DuplicateMessage``.
        """
        now = time.monotonic()
        s = self.settings

        if priority == Priority.critical:
            self._critical.check(now, s.sms_rate_limit_critical_hour, 3600, "hour (critical)")
            self._critical.add(now)
            return None

        self._normal.check(now, s.sms_rate_limit_minute, 60, "min")
        self._normal.check(now, s.sms_rate_limit_hour, 3600, "hour")

        self._dedup = {k: v for k, v in self._dedup.items() if v > now}
        key = self.dedup_key(number, message, alert_key)
        if key in self._dedup:
            raise DuplicateMessage(
                f"Duplicate SMS to {number} within {s.sms_dedup_seconds}s — skipped"
            )
        if is_queued is not None and is_queued(key):
            raise DuplicateMessage(f"Duplicate SMS to {number} already queued in outbox — skipped")

        self._normal.add(now)
        self._dedup[key] = now + s.sms_dedup_seconds
        return key

    def release_dedup(self, key: str) -> None:
        """Forget a dedup reservation (the send failed and was not queued),
        so a legitimate retry can land after recovery. The rate slot stays
        consumed: a broken caller can't retry-storm during a modem outage."""
        self._dedup.pop(key, None)

    def budget(self) -> dict:
        """Live rate-limit usage per window, plus the dedup window."""
        now, s = time.monotonic(), self.settings

        def window(win: SlidingWindow, limit: int, span: int) -> dict:
            used, frees_in = win.usage(now, span)
            return {"used": used, "limit": limit,
                    "frees_in_seconds": frees_in if used >= limit else 0,
                    "oldest_leaves_in_seconds": frees_in}

        return {
            "normal_minute": window(self._normal, s.sms_rate_limit_minute, 60),
            "normal_hour": window(self._normal, s.sms_rate_limit_hour, 3600),
            "critical_hour": window(self._critical, s.sms_rate_limit_critical_hour, 3600),
            "dedup_seconds": s.sms_dedup_seconds,
            "dedup_active": sum(1 for v in self._dedup.values() if v > now),
        }
