from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from kfcquant.config import Settings
from kfcquant.models import CandidateScore, FactorBreakdown
from kfcquant.policies import SchedulePolicy


def is_shenzhen_shanghai_main_board(ts_code: str) -> bool:
    """Backward-compatible import; stock-pool ownership lives in UniversePolicy."""
    from kfcquant.strategy.universe import is_shenzhen_shanghai_main_board as matches_main_board

    return matches_main_board(ts_code)


def trading_minutes_elapsed(at: datetime, schedule: SchedulePolicy | None = None) -> int:
    """Backward-compatible import; session feature timing lives in FeaturePipeline."""
    from kfcquant.strategy.features import trading_minutes_elapsed as elapsed

    return elapsed(at, schedule)


def _percentile(series: pd.Series) -> pd.Series:
    if series.nunique(dropna=True) <= 1:
        return pd.Series(50.0, index=series.index)
    return series.rank(method="average", pct=True).fillna(0.0) * 100.0


@dataclass
class ScoringResult:
    candidates: list[CandidateScore]
    eligible_count: int
    exclusion_counts: dict[str, int]


class ScoringService:
    """Applies scores and risk to already selected, calculated market features."""

    def __init__(self, settings: Settings):
        self.settings = settings

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

    def score_preclose_features(
        self,
        run_id: str,
        features: pd.DataFrame,
        risk_events: pd.DataFrame | None = None,
        unprocessed_official_codes: set[str] | None = None,
        morning_codes: set[str] | None = None,
        exclusion_counts: dict[str, int] | None = None,
    ) -> ScoringResult:
        frame = features.copy()
        exclusions = exclusion_counts or {"eligible": len(frame)}
        if frame.empty:
            return ScoringResult([], 0, exclusions)

        positive = (
            0.25 * _percentile(frame["ret_5d"])
            + 0.15 * _percentile(frame["ret_20d"])
            + 0.20 * _percentile(frame["intraday_strength"])
            + 0.15 * _percentile(frame["close_location"])
            + 0.15 * _percentile(frame["projected_volume_ratio"])
            + 0.10 * _percentile(np.log1p(frame["median_amount_20d"]))
        )
        abnormal_volume = np.clip((frame["projected_volume_ratio"] - 3.0) / 2.0, 0.0, 1.0)
        penalty = (
            8.0 * _percentile(frame["volatility_20d"]) / 100.0
            + 4.0 * _percentile(frame["gap_abs"]) / 100.0
            + 4.0 * frame["limit_proximity"]
            + 4.0 * abnormal_volume
        )
        frame["positive_score"] = positive
        frame["risk_penalty"] = np.clip(penalty, 0.0, 20.0)
        frame["technical_score"] = np.clip(
            0.9 * (frame["positive_score"] - frame["risk_penalty"]), 0.0, 90.0
        )

        events_by_code: dict[str, list[dict[str, object]]] = {}
        if risk_events is not None and not risk_events.empty:
            for event in risk_events.to_dict("records"):
                code = event.get("ts_code")
                if code:
                    events_by_code.setdefault(str(code), []).append(event)
        unprocessed = unprocessed_official_codes or set()
        morning = morning_codes or set()

        staged: list[dict[str, object]] = []
        for row in frame.to_dict("records"):
            code = str(row["ts_code"])
            related = events_by_code.get(code, [])
            positive_events = [event for event in related if str(event.get("direction")) == "positive"]
            negative_events = [event for event in related if str(event.get("direction")) == "negative"]
            news_score = min(sum(float(event.get("confidence", 0.0)) * 2.5 for event in positive_events), 7.0)
            news_penalty = min(
                sum(
                    {"low": 1.0, "medium": 3.0, "high": 7.0, "critical": 15.0}.get(
                        str(event.get("severity")), 0.0
                    )
                    for event in negative_events
                ),
                20.0,
            )
            confirmed = (
                code in morning
                and float(row["intraday_strength"]) > 0
                and float(row["close_location"]) >= 0.5
            )
            row["news_score"] = news_score
            row["continuity_score"] = 3.0 if confirmed else 0.0
            row["morning_status"] = "confirmed" if confirmed else ("invalidated" if code in morning else "new")
            row["opportunity_score"] = float(
                np.clip(row["technical_score"] + news_score + row["continuity_score"] - news_penalty, 0.0, 100.0)
            )
            reasons: list[str] = []
            if code in unprocessed:
                reasons.append("存在尚未完成抽取的官方公告")
            for event in related:
                if bool(event.get("hard_block")):
                    reasons.append(f"{event.get('event_type')}: {event.get('evidence')}")
            row["blocked"] = bool(reasons)
            row["block_reasons"] = reasons
            row["risk_event_ids"] = [str(event["event_id"]) for event in related]
            staged.append(row)

        staged.sort(key=lambda item: (bool(item["blocked"]), -float(item["opportunity_score"]), str(item["ts_code"])))
        candidates: list[CandidateScore] = []
        for index, row in enumerate(staged, start=1):
            breakdown = FactorBreakdown.model_validate(
                {field: row[field] for field in FactorBreakdown.model_fields if field in row}
            )
            candidates.append(
                CandidateScore(
                    run_id=run_id,
                    ts_code=str(row["ts_code"]),
                    name=str(row["name"]),
                    rank=index,
                    opportunity_score=round(float(row["opportunity_score"]), 4),
                    factor_breakdown=breakdown,
                    risk_event_ids=list(row["risk_event_ids"]),
                    blocked=bool(row["blocked"]),
                    block_reasons=list(row["block_reasons"]),
                    quote_at=row["quote_at"],
                )
            )
        return ScoringResult(
            candidates=candidates[: self.settings.selection.candidate_limit],
            eligible_count=len(frame),
            exclusion_counts=exclusions,
        )

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
        frame = features.copy()
        exclusions = exclusion_counts or {"eligible": len(frame)}
        if frame.empty:
            return ScoringResult([], 0, exclusions)
        base = (
            0.20 * _percentile(frame["ret_1d"])
            + 0.25 * _percentile(frame["ret_5d"])
            + 0.15 * _percentile(frame["ret_20d"])
            + 0.15 * _percentile(frame["close_location"])
            + 0.15 * _percentile(frame["projected_volume_ratio"])
            + 0.10 * _percentile(np.log1p(frame["median_amount_20d"]))
        )
        volatility_penalty = 10.0 * _percentile(frame["volatility_20d"]) / 100.0
        frame["positive_score"] = base
        frame["risk_penalty"] = volatility_penalty
        frame["technical_score"] = np.clip(0.9 * (base - volatility_penalty), 0.0, 90.0)

        events_by_code: dict[str, list[dict[str, object]]] = {}
        if risk_events is not None and not risk_events.empty:
            for event in risk_events.to_dict("records"):
                if event.get("ts_code"):
                    events_by_code.setdefault(str(event["ts_code"]), []).append(event)
        unprocessed = unprocessed_official_codes or set()
        staged: list[dict[str, object]] = []
        for row in frame.to_dict("records"):
            code = str(row["ts_code"])
            related = events_by_code.get(code, [])
            positives = [event for event in related if str(event.get("direction")) == "positive"]
            negatives = [event for event in related if str(event.get("direction")) == "negative"]
            row["news_score"] = min(sum(float(event.get("confidence", 0.0)) * 3.0 for event in positives), 10.0)
            negative_penalty = min(
                sum(
                    {"low": 1.0, "medium": 3.0, "high": 7.0, "critical": 15.0}.get(
                        str(event.get("severity")), 0.0
                    )
                    for event in negatives
                ),
                20.0,
            )
            reasons = [
                f"{event.get('event_type')}: {event.get('evidence')}" for event in related if event.get("hard_block")
            ]
            if code in unprocessed:
                reasons.append("存在尚未完成抽取的官方公告")
            row["opportunity_score"] = float(
                np.clip(row["technical_score"] + row["news_score"] - negative_penalty, 0.0, 100.0)
            )
            row["blocked"] = bool(reasons)
            row["block_reasons"] = reasons
            row["risk_event_ids"] = [str(event["event_id"]) for event in related]
            staged.append(row)
        staged.sort(key=lambda item: (bool(item["blocked"]), -float(item["opportunity_score"]), str(item["ts_code"])))
        candidates: list[CandidateScore] = []
        for rank, row in enumerate(staged, start=1):
            candidates.append(
                CandidateScore(
                    run_id=run_id,
                    ts_code=str(row["ts_code"]),
                    name=str(row["name"]),
                    rank=rank,
                    opportunity_score=round(float(row["opportunity_score"]), 4),
                    factor_breakdown=FactorBreakdown.model_validate(
                        {field: row[field] for field in FactorBreakdown.model_fields if field in row}
                    ),
                    risk_event_ids=list(row["risk_event_ids"]),
                    blocked=bool(row["blocked"]),
                    block_reasons=list(row["block_reasons"]),
                    quote_at=row["quote_at"],
                )
            )
        return ScoringResult(
            candidates[: self.settings.selection.candidate_limit], len(frame), exclusions
        )
