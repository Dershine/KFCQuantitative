from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Application time source used by live and replay use cases."""

    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class SystemClock:
    """Wall-clock time in the configured application timezone."""

    timezone: tzinfo

    def __post_init__(self) -> None:
        if self.timezone is None:
            raise ValueError("SystemClock timezone must not be None")

    def now(self) -> datetime:
        return datetime.now(self.timezone)


@dataclass(frozen=True, slots=True)
class ReplayClock:
    """A fixed, explicit point in time for deterministic replay."""

    current: datetime

    def __post_init__(self) -> None:
        if self.current.tzinfo is None or self.current.utcoffset() is None:
            raise ValueError("ReplayClock current time must be timezone-aware")

    def now(self) -> datetime:
        return self.current
