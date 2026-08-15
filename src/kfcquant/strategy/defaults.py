from __future__ import annotations

from kfcquant.config import Settings
from kfcquant.models import SignalKind
from kfcquant.services.scoring import ScoringService
from kfcquant.strategy.contracts import (
    StrategyContext,
    StrategyIdentity,
    StrategyRequirements,
    StrategyResult,
)
from kfcquant.strategy.registry import StrategyRegistry


class MorningWatchlistStrategy:
    signal_kind = SignalKind.MORNING_WATCHLIST
    requirements = StrategyRequirements()

    def __init__(self, settings: Settings) -> None:
        self.identity = StrategyIdentity("morning-watchlist", settings.strategy_version_morning)
        self._scoring = ScoringService(settings)

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        if context.signal_kind != self.signal_kind:
            raise ValueError(f"morning strategy cannot evaluate {context.signal_kind.value}")
        result = self._scoring.score_morning(
            context.run_id,
            context.securities,
            context.bars,
            context.as_of,
            context.risk_events,
            set(context.unprocessed_official_codes),
        )
        return StrategyResult(result.candidates, result.eligible_count, result.exclusion_counts)


class PrecloseEntryStrategy:
    signal_kind = SignalKind.PRECLOSE_ENTRY
    requirements = StrategyRequirements(requires_quotes=True, requires_previous_signals=True)

    def __init__(self, settings: Settings) -> None:
        self.identity = StrategyIdentity("preclose-entry", settings.strategy_version_preclose)
        self._scoring = ScoringService(settings)

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        if context.signal_kind != self.signal_kind:
            raise ValueError(f"pre-close strategy cannot evaluate {context.signal_kind.value}")
        result = self._scoring.score(
            context.run_id,
            context.securities,
            context.bars,
            context.quotes,
            context.as_of,
            context.risk_events,
            set(context.unprocessed_official_codes),
            set(context.previous_signal_codes),
        )
        return StrategyResult(result.candidates, result.eligible_count, result.exclusion_counts)


def build_default_strategy_registry(settings: Settings) -> StrategyRegistry:
    """Composition root for the built-in research strategies."""

    return StrategyRegistry([MorningWatchlistStrategy(settings), PrecloseEntryStrategy(settings)])
