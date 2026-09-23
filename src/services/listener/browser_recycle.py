import time
from typing import Callable, Optional


def read_available_memory_mb(meminfo_path: str = "/proc/meminfo") -> Optional[float]:
    """Return Linux host-available memory as seen by the container."""
    try:
        with open(meminfo_path, "r", encoding="ascii") as meminfo:
            for line in meminfo:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        return None
    return None


class BrowserRecyclePolicy:
    """Decide when a long-running browser should be rebuilt."""

    def __init__(
        self,
        interval_seconds: float = 21600.0,
        min_available_memory_mb: float = 256.0,
        memory_check_interval_seconds: float = 10.0,
        memory_recycle_min_age_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        memory_reader: Callable[[], Optional[float]] = read_available_memory_mb,
    ):
        self.interval_seconds = max(0.0, float(interval_seconds or 0.0))
        self.min_available_memory_mb = max(0.0, float(min_available_memory_mb or 0.0))
        self.memory_check_interval_seconds = max(1.0, float(memory_check_interval_seconds or 1.0))
        self.memory_recycle_min_age_seconds = max(0.0, float(memory_recycle_min_age_seconds or 0.0))
        self.clock = clock
        self.memory_reader = memory_reader
        self.browser_started_at = 0.0
        self.last_memory_check_at = 0.0

    def mark_browser_started(self) -> None:
        self.browser_started_at = self.clock()
        self.last_memory_check_at = 0.0

    def recycle_reason(self) -> Optional[str]:
        if self.browser_started_at <= 0:
            return None

        now = self.clock()
        elapsed = now - self.browser_started_at
        if self.interval_seconds > 0 and elapsed >= self.interval_seconds:
            return (
                "Chrome browser recycle interval reached "
                f"({elapsed:.0f}s >= {self.interval_seconds:.0f}s)"
            )

        if (
            self.min_available_memory_mb <= 0
            or elapsed < self.memory_recycle_min_age_seconds
            or (
                self.last_memory_check_at > 0
                and now - self.last_memory_check_at < self.memory_check_interval_seconds
            )
        ):
            return None

        self.last_memory_check_at = now
        available_memory_mb = self.memory_reader()
        if (
            available_memory_mb is not None
            and available_memory_mb < self.min_available_memory_mb
        ):
            return (
                "Host available memory is low "
                f"({available_memory_mb:.0f} MiB < {self.min_available_memory_mb:.0f} MiB)"
            )
        return None
