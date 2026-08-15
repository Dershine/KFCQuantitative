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
from kfcquant.strategy.features import FeaturePipeline
from kfcquant.strategy.registry import StrategyRegistry
from kfcquant.strategy.universe import UniversePolicy


class MorningWatchlistStrategy:
    signal_kind = SignalKind.MORNING_WATCHLIST
    requirements = StrategyRequirements()

    def __init__(self, settings: Settings) -> None:
        self.identity = StrategyIdentity("morning-watchlist", settings.strategy_version_morning)
        self._universe = UniversePolicy.from_settings(settings)
        self._features = FeaturePipeline.from_settings(settings)
        self._scoring = ScoringService(settings)

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        if context.signal_kind != self.signal_kind:
            raise ValueError(f"morning strategy cannot evaluate {context.signal_kind.value}")
        universe = self._universe.select(context.securities, context.bars)
        features = self._features.build_morning(universe, context.as_of)
        exclusions = self._scoring.combine_exclusion_counts(
            universe.exclusion_counts, features.exclusion_counts, len(features.frame)
        )
        result = self._scoring.score_morning_features(
            context.run_id,
            features.frame,
            context.risk_events,
            set(context.unprocessed_official_codes),
            exclusions,
        )
        return StrategyResult(result.candidates, result.eligible_count, result.exclusion_counts)


class PrecloseEntryStrategy:
    signal_kind = SignalKind.PRECLOSE_ENTRY
    requirements = StrategyRequirements(requires_quotes=True, requires_previous_signals=True)

    def __init__(self, settings: Settings) -> None:
        self.identity = StrategyIdentity("preclose-entry", settings.strategy_version_preclose)
        self._universe = UniversePolicy.from_settings(settings)
        self._features = FeaturePipeline.from_settings(settings)
        self._scoring = ScoringService(settings)

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        if context.signal_kind != self.signal_kind:
            raise ValueError(f"pre-close strategy cannot evaluate {context.signal_kind.value}")
        universe = self._universe.select(context.securities, context.bars)
        features = self._features.build_preclose(universe, context.quotes, context.as_of)
        exclusions = self._scoring.combine_exclusion_counts(
            universe.exclusion_counts, features.exclusion_counts, len(features.frame)
        )
        result = self._scoring.score_preclose_features(
            context.run_id,
            features.frame,
            context.risk_events,
            set(context.unprocessed_official_codes),
            set(context.previous_signal_codes),
            exclusions,
        )
        return StrategyResult(result.candidates, result.eligible_count, result.exclusion_counts)


def build_default_strategy_registry(settings: Settings) -> StrategyRegistry:
    """Composition root for the built-in research strategies."""

    return StrategyRegistry([MorningWatchlistStrategy(settings), PrecloseEntryStrategy(settings)])
