from __future__ import annotations

from kfcquant.config import Settings
from kfcquant.models import SignalKind
from kfcquant.strategy.contracts import (
    StrategyContext,
    StrategyIdentity,
    StrategyRequirements,
    StrategyResult,
)
from kfcquant.strategy.features import MORNING_FEATURE_SCHEMA, PRECLOSE_FEATURE_SCHEMA, FeaturePipeline
from kfcquant.strategy.registry import StrategyRegistry
from kfcquant.strategy.universe import UniversePolicy
from kfcquant.strategy_identity import StrategyParameterSnapshot


def _strategy_parameter_snapshot(settings: Settings, signal_kind: SignalKind) -> StrategyParameterSnapshot:
    parameters: dict[str, object] = {
        "universe": {
            "min_listing_trading_days": settings.min_listing_trading_days,
            "min_median_amount_20d": settings.min_median_amount_20d,
        },
        "selection": settings.selection.model_dump(mode="json"),
        "risk": {"news_lookback_trading_days": settings.news_lookback_trading_days},
    }
    if signal_kind == SignalKind.MORNING_WATCHLIST:
        parameters["features"] = {
            "schema": MORNING_FEATURE_SCHEMA.version,
            "market_close": settings.schedule.market_close,
        }
    else:
        parameters["features"] = {
            "schema": PRECLOSE_FEATURE_SCHEMA.version,
            "quote_freshness_seconds": settings.quote_freshness_seconds,
            "limit_distance_fraction": settings.limit_distance_fraction,
            "market_morning_open": settings.schedule.market_morning_open,
            "market_morning_close": settings.schedule.market_morning_close,
            "market_afternoon_open": settings.schedule.market_afternoon_open,
            "market_close": settings.schedule.market_close,
        }
    return StrategyParameterSnapshot.from_mapping(parameters)


class MorningWatchlistStrategy:
    signal_kind = SignalKind.MORNING_WATCHLIST
    requirements = StrategyRequirements()

    def __init__(self, settings: Settings) -> None:
        from kfcquant.services.scoring import ScoringService

        self.identity = StrategyIdentity(
            "morning-watchlist",
            settings.strategy_version_morning,
            _strategy_parameter_snapshot(settings, self.signal_kind),
        )
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
        from kfcquant.services.scoring import ScoringService

        self.identity = StrategyIdentity(
            "preclose-entry",
            settings.strategy_version_preclose,
            _strategy_parameter_snapshot(settings, self.signal_kind),
        )
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
