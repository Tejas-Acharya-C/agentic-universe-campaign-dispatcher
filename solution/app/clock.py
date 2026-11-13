"""Simulated and fake clock abstractions for timezone-aware IST timing."""

from datetime import datetime, timedelta, timezone
import time
from typing import Protocol, runtime_checkable

# IST timezone: UTC + 5 hours 30 minutes
IST = timezone(timedelta(hours=5, minutes=30))
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@runtime_checkable
class Clock(Protocol):
    """Protocol for clocks returning timezone-aware IST datetime."""

    def now(self) -> datetime:
        """Return the current time in IST."""
        ...


class SimClock:
    """Clock that calculates simulated time based on the mock provider's clock epoch and speed."""

    def __init__(self, sim_epoch: datetime, real_epoch: float, speed: float):
        if sim_epoch.tzinfo is None:
            self.sim_epoch = sim_epoch.replace(tzinfo=IST)
        else:
            self.sim_epoch = sim_epoch.astimezone(IST)
        self.real_epoch = float(real_epoch)
        self.speed = float(speed)

    def sync_from_payload(self, payload: dict) -> None:
        """Update clock parameters from a GET /v1/clock response payload."""
        sim_epoch = datetime.fromisoformat(payload["sim_epoch"])
        if sim_epoch.tzinfo is None:
            self.sim_epoch = sim_epoch.replace(tzinfo=IST)
        else:
            self.sim_epoch = sim_epoch.astimezone(IST)
        self.real_epoch = float(payload["real_epoch"])
        self.speed = float(payload["speed"])

    @classmethod
    def from_payload(cls, payload: dict) -> "SimClock":
        """Instantiate a SimClock from a GET /v1/clock response payload."""
        sim_epoch = datetime.fromisoformat(payload["sim_epoch"])
        return cls(
            sim_epoch=sim_epoch,
            real_epoch=float(payload["real_epoch"]),
            speed=float(payload["speed"]),
        )

    def now(self) -> datetime:
        """Compute current simulated time in IST."""
        elapsed_real = time.time() - self.real_epoch
        sim_now = self.sim_epoch + timedelta(seconds=elapsed_real * self.speed)
        return sim_now.astimezone(IST)

    def sim_to_real_seconds(self, sim_seconds: float) -> float:
        """Convert simulated seconds to real wall-clock seconds."""
        if self.speed <= 0:
            return 0.0
        return max(0.0, sim_seconds / self.speed)


class FakeClock:
    """Deterministic in-memory clock for testing without sleeps."""

    def __init__(self, current: datetime | str):
        if isinstance(current, str):
            dt = datetime.fromisoformat(current)
        else:
            dt = current
        if dt.tzinfo is None:
            self.current = dt.replace(tzinfo=IST)
        else:
            self.current = dt.astimezone(IST)

    def now(self) -> datetime:
        """Return the current simulated time."""
        return self.current

    def advance(
        self,
        seconds: float = 0,
        minutes: float = 0,
        hours: float = 0,
        days: float = 0,
    ) -> datetime:
        """Advance the fake clock and return the updated time."""
        self.current = self.current + timedelta(
            seconds=seconds, minutes=minutes, hours=hours, days=days
        )
        return self.current

