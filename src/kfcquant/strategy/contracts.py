from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

import pandas as pd

from kfcquant.models import CandidateScore, SignalKind
from kfcquant.strategy_identity import StrategyIdentity


@dataclass(frozen=True, slots=True)
class StrategyRequirements:
    """Inputs a strategy expects the application layer to collect."""

    requires_quotes: bool = False
    requires_previous_signals: bool = False


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Time-bounded inputs supplied to every Strategy implementation."""

    run_id: str
    signal_kind: SignalKind
    as_of: datetime
    information_cutoff: datetime
    securities: pd.DataFrame
    bars: pd.DataFrame
    quotes: pd.DataFrame = field(default_factory=pd.DataFrame)
    risk_events: pd.DataFrame | None = None
    unprocessed_official_codes: frozenset[str] = frozenset()
    previous_signal_codes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must include timezone information")
        if self.information_cutoff.tzinfo is None or self.information_cutoff.utcoffset() is None:
            raise ValueError("information_cutoff must include timezone information")
        if self.information_cutoff > self.as_of:
            raise ValueError("information_cutoff cannot be after as_of")


@dataclass(frozen=True, slots=True)
class StrategyResult:
    candidates: list[CandidateScore]
    eligible_count: int
    exclusion_counts: dict[str, int]
    diagnostics: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class Strategy(Protocol):
    signal_kind: SignalKind
    identity: StrategyIdentity
    requirements: StrategyRequirements

    def evaluate(self, context: StrategyContext) -> StrategyResult: ...
