from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from kfcquant.config import Settings
from kfcquant.models import CandidateScore, FactorBreakdown, SignalKind
from kfcquant.policies import SchedulePolicy
from kfcquant.strategy.risk import RiskPolicy
from kfcquant.strategy.scoring import ScoreModel


def is_shenzhen_shanghai_main_board(ts_code: str) -> bool:
    """Backward-compatible import; stock-pool ownership lives in UniversePolicy."""
    from kfcquant.strategy.universe import is_shenzhen_shanghai_main_board as matches_main_board

    return matches_main_board(ts_code)


def trading_minutes_elapsed(at: datetime, schedule: SchedulePolicy | None = None) -> int:
    """Backward-compatible import; session feature timing lives in FeaturePipeline."""
    from kfcquant.strategy.features import trading_minutes_elapsed as elapsed

    return elapsed(at, schedule)


@dataclass
class ScoringResult:
    candidates: list[CandidateScore]
    eligible_count: int
    exclusion_counts: dict[str, int]


class ScoringService:
    """Compatibility facade that composes technical score, risk, and selection policies."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.score_model = ScoreModel()

    @staticmethod
    def combine_exclusion_counts(
        universe_counts: dict[str, int],
        feature_counts: dict[str, int],
        eligible_count: int,
    ) -> dict[str, int]:
        combined = {key: value for key, value in universe_counts.items() if key != "eligible"}
        for key, value in feature_counts.items():
            if key != "eligible_features":
                combined[key] = combined.get(key, 0) + value
        combined["eligible"] = eligible_count
        return combined

    def score(
        self,
        run_id: str,
        securities: pd.DataFrame,
        bars: pd.DataFrame,
        quotes: pd.DataFrame,
        as_of: datetime,
        risk_events: pd.DataFrame | None = None,
        unprocessed_official_codes: set[str] | None = None,
        morning_codes: set[str] | None = None,
    ) -> ScoringResult:
        """Compatibility entry point for callers outside the Strategy registry."""
        from kfcquant.strategy.features import FeaturePipeline
        from kfcquant.strategy.universe import UniversePolicy

        universe = UniversePolicy.from_settings(self.settings).select(securities, bars)
        features = FeaturePipeline.from_settings(self.settings).build_preclose(universe, quotes, as_of)
        return self.score_preclose_features(
            run_id,
            features.frame,
            risk_events,
            unprocessed_official_codes,
            morning_codes,
            self.combine_exclusion_counts(universe.exclusion_counts, features.exclusion_counts, len(features.frame)),
        )

    def _build_candidates(
        self,
        run_id: str,
        scored_features: pd.DataFrame,
        signal_kind: SignalKind,
        risk_policy: RiskPolicy,
        morning_codes: set[str] | None = None,
    ) -> list[CandidateScore]:
        morning = morning_codes or set()
        candidates: list[CandidateScore] = []
        for row in scored_features.to_dict("records"):
            code = str(row["ts_code"])
            assessment = risk_policy.assess(code, signal_kind)
            continuity_score = 0.0
            morning_status = "not_applicable"
            if signal_kind == SignalKind.PRECLOSE_ENTRY:
                confirmed = (
                    code in morning
                    and float(row["intraday_strength"]) > 0
                    and float(row["close_location"]) >= 0.5
                )
                continuity_score = 3.0 if confirmed else 0.0
                morning_status = "confirmed" if confirmed else ("invalidated" if code in morning else "new")
            opportunity_score = float(
                np.clip(
                    float(row["technical_score"])
                    + assessment.news_score
                    + continuity_score
                    - assessment.news_penalty,
                    0.0,
                    100.0,
                )
            )
            factor_values = {
                field: row[field]
                for field in FactorBreakdown.model_fields
                if field in row
            }
            factor_values.update(
                news_score=assessment.news_score,
                continuity_score=continuity_score,
                morning_status=morning_status,
            )
            candidates.append(
                CandidateScore(
                    run_id=run_id,
                    ts_code=code,
                    name=str(row["name"]),
                    rank=1,
                    opportunity_score=round(opportunity_score, 4),
                    factor_breakdown=FactorBreakdown.model_validate(factor_values),
                    risk_event_ids=list(assessment.risk_event_ids),
                    blocked=assessment.blocked,
                    block_reasons=list(assessment.block_reasons),
                    quote_at=row["quote_at"],
                )
            )
        return self.settings.selection.rank_candidates(candidates)

    def score_preclose_features(
        self,
        run_id: str,
        features: pd.DataFrame,
        risk_events: pd.DataFrame | None = None,
        unprocessed_official_codes: set[str] | None = None,
        morning_codes: set[str] | None = None,
        exclusion_counts: dict[str, int] | None = None,
    ) -> ScoringResult:
        exclusions = exclusion_counts or {"eligible": len(features)}
        if features.empty:
            return ScoringResult([], 0, exclusions)
        scored = self.score_model.score_preclose(features)
        candidates = self._build_candidates(
            run_id,
            scored,
            SignalKind.PRECLOSE_ENTRY,
            RiskPolicy(risk_events, unprocessed_official_codes),
            morning_codes,
        )
        return ScoringResult(candidates, len(features), exclusions)

    def score_morning(
        self,
        run_id: str,
        securities: pd.DataFrame,
        bars: pd.DataFrame,
        as_of: datetime,
        risk_events: pd.DataFrame | None = None,
        unprocessed_official_codes: set[str] | None = None,
    ) -> ScoringResult:
        """Compatibility entry point for callers outside the Strategy registry."""
        from kfcquant.strategy.features import FeaturePipeline
        from kfcquant.strategy.universe import UniversePolicy

        universe = UniversePolicy.from_settings(self.settings).select(securities, bars)
        features = FeaturePipeline.from_settings(self.settings).build_morning(universe, as_of)
        return self.score_morning_features(
            run_id,
            features.frame,
            risk_events,
            unprocessed_official_codes,
            self.combine_exclusion_counts(universe.exclusion_counts, features.exclusion_counts, len(features.frame)),
        )

    def score_morning_features(
        self,
        run_id: str,
        features: pd.DataFrame,
        risk_events: pd.DataFrame | None = None,
        unprocessed_official_codes: set[str] | None = None,
        exclusion_counts: dict[str, int] | None = None,
    ) -> ScoringResult:
        exclusions = exclusion_counts or {"eligible": len(features)}
        if features.empty:
            return ScoringResult([], 0, exclusions)
        scored = self.score_model.score_morning(features)
        candidates = self._build_candidates(
            run_id,
            scored,
            SignalKind.MORNING_WATCHLIST,
            RiskPolicy(risk_events, unprocessed_official_codes),
        )
        return ScoringResult(candidates, len(features), exclusions)
